import os

from chaonan_src.doc_retrieval_experiment import DocRetrievalExperimentSpiral, DocRetrievalExperiment, \
    DocRetrievalExperimentTwoStep
from chaonan_src._doc_retrieval.item_rules_spiral import ItemRuleBuilderSpiral, ItemRuleBuilderNoPageview
from chaonan_src._doc_retrieval.item_rules_test import ItemRuleBuilderTest
from chaonan_src.doc_retrieval_experiment_client import DocRetrievalClient
from sentence_retrieval.simple_nnmodel import get_score_multihop
from utils import common
import utils
import config
from utils.tokenize_fever import tokenized_claim
from utils import c_scorer
from typing import Dict
from sentence_retrieval import simple_nnmodel
from simi_sampler_nli_v0 import simi_sampler
import nli.mesim_wn_simi_v1_1
import nli.mesim_wn_simi_v1_2
import copy
import json
import numpy as np

PIPELINE_DIR = config.RESULT_PATH / "pipeline_r"

default_model_path_dict: Dict[str, str] = {
    'sselector': config.PRO_ROOT / 'saved_models/saved_sselector/i(57167)_epoch(6)_(tra_score:0.8850885088508851|raw_acc:1.0|pr:0.3834395939593578|rec:0.8276327632763276|f1:0.5240763176570098)_epoch',
    'sselector_1': config.PRO_ROOT / 'saved_models/saved_sselector/i(77083)_epoch(7)_(tra_score:0.8841384138413841|raw_acc:1.0|pr:0.3964771477147341|rec:0.8262076207620762|f1:0.5358248492912955)_epoch',
    'sselector_2': config.PRO_ROOT / 'saved_models/saved_sselector/i(58915)_epoch(7)_(tra_score:0.8838383838383839|raw_acc:1.0|pr:0.39771352135209675|rec:0.8257575757575758|f1:0.5368577222846761)_epoch',

    'nli': config.PRO_ROOT / 'saved_models/saved_v1_2/i(77000)_epoch(11)_dev(0.6601160116011601)_loss(1.1138329989302813)_seed(12)',
    'nli_1': config.PRO_ROOT / 'saved_models/saved_v1_2/i(74000)_epoch(11)_dev(0.6546654665466547)_loss(1.3616722218274284)_seed(12)',
    'nli_2': config.PRO_ROOT / 'saved_models/saved_v1_2/i(85500)_epoch(13)_dev(0.6588658865886589)_loss(1.6495013034442674)_seed(12)',
    'nli_3': config.PRO_ROOT / 'saved_models/saved_v1_2/i(85500)_epoch(13)_dev(0.6578657865786579)_loss(1.7740270915371452)_seed(12)',
    'nli_4': config.PRO_ROOT / 'saved_models/saved_v1_2/i(77000)_epoch(11)_dev(0.6552155215521552)_loss(1.5343008287871691)_seed(12)',
}


def merge_sent_results(sent_r_list):
    r_len = len(sent_r_list[0])
    for sent_r in sent_r_list:
        assert len(sent_r) == r_len

    new_list = copy.deepcopy(sent_r_list[0])
    for i in range(r_len):
        prob_list = []
        score_list = []
        for sent_r in sent_r_list:
            assert sent_r[i]['selection_id'] == new_list[i]['selection_id']
            prob_list.append(sent_r[i]['prob'])
            score_list.append(sent_r[i]['score'])
        # assert len(prob_list) ==
        new_list[i]['prob'] = float(np.mean(prob_list))
        new_list[i]['score'] = float(np.mean(score_list))

    return new_list


id2label = {
    0: "SUPPORTS",
    1: "REFUTES",
    2: "NOT ENOUGH INFO",
}


def merge_nli_results(nli_r_list):
    r_len = len(nli_r_list[0])
    for nli_r in nli_r_list:
        assert len(nli_r) == r_len

    new_list = copy.deepcopy(nli_r_list[0])
    logits_list = []
    for i in range(r_len):
        logits_current_logits_list = []
        for nli_r in nli_r_list:
            assert nli_r[i]['id'] == new_list[i]['id']
            logits_current_logits_list.append(np.asarray(nli_r[i]['logits'], dtype=np.float32)) # [(3)]
        logits_current_logits = np.stack(logits_current_logits_list, axis=0)    # [num, 3]
        current_mean_logits = np.mean(logits_current_logits, axis=0)        # [3]
        logits_list.append(current_mean_logits)

    logits = np.stack(logits_list, axis=0)   # (len, 3)
    y_ = np.argmax(logits, axis=1)  # (len)
    assert y_.shape[0] == len(new_list)

    for i in range(r_len):
        new_list[i]['predicted_label'] = id2label[y_[i]]

    return new_list


default_steps = {
    's1.tokenizing': {
        'do': True,
        'out_file': 'None'  # if false, we will directly use the out_file, This out file for downstream
    },
    's2.1doc_retri': {
        'do': True,
        'out_file': 'None'  # if false, we will directly use the out_file, for downstream
    },
    's3.1sen_select': {
        'do': True,
        'out_file': 'None'
    },
    's4.2doc_retri': {
        'do': True,
        'out_file': 'None'
    },
    's5.2sen_select': {
        'do': True,
        'out_file': 'None'
    },
    's6.nli': {
        'do': True,
        'out_file': 'None'
    }
}


class HAONAN_DOCRETRI_OBJECT:
    def __init__(self):
        self.instance = None


def init_haonan_docretri_object(object, method='server'):
    if method == 'server':
        object.instance = DocRetrievalClient
    else:
        item_rb_selector = {
            'word_freq': ItemRuleBuilderTest,
            'pageview': ItemRuleBuilderSpiral,
            'nopageview': ItemRuleBuilderNoPageview,
        }[method]
        if object.instance is None:
            object.instance = DocRetrievalExperimentTwoStep(item_rb_selector())


def pipeline(in_file, eval_file=None,
             model_path_dict=default_model_path_dict,
             steps=default_steps):
    """
    :param in_file: The raw input file.
    :param eval_file: Whether to provide evaluation along the line.
    :return:
    """
    sentence_retri_1_scale_prob = 0.5
    sentence_retri_2_scale_prob = 0.9
    sent_retri_1_top_k = 5
    sent_retri_2_top_k = 1

    sent_prob_for_2doc = 0.1
    sent_topk_for_2doc = 5
    enhance_retri_1_scale_prob = -1

    build_submission = True

    doc_retrieval_method = 'word_freq'

    haonan_docretri_object = HAONAN_DOCRETRI_OBJECT()

    if not PIPELINE_DIR.exists():
        PIPELINE_DIR.mkdir()

    if steps['s1.tokenizing']['do']:
        time_stamp = utils.get_current_time_str()
        current_pipeline_dir = PIPELINE_DIR / f"{time_stamp}_r"
    else:
        current_pipeline_dir = steps['s1.tokenizing']['out_file'].parent

    print("Current Result Root:", current_pipeline_dir)

    if not current_pipeline_dir.exists():
        current_pipeline_dir.mkdir()

    eval_list = common.load_jsonl(eval_file) if eval_file is not None else None

    in_file_stem = in_file.stem
    tokenized_file = current_pipeline_dir / f"t_{in_file_stem}.jsonl"

    # Save code into directory
    script_name = os.path.basename(__file__)
    with open(os.path.join(str(current_pipeline_dir), script_name), 'w') as out_f, open(__file__, 'r') as it:
        out_f.write(it.read())
        out_f.flush()

    # Tokenizing.
    print("Step 1. Tokenizing.")
    if steps['s1.tokenizing']['do']:
        tokenized_claim(in_file, tokenized_file)  # Auto Saved
        print("Tokenized file saved to:", tokenized_file)
    else:
        tokenized_file = steps['s1.tokenizing']['out_file']
        print("Use preprocessed file:", tokenized_file)
    # Tokenizing End.

    # First Document retrieval.
    print("Step 2. First Document Retrieval")

    if steps['s2.1doc_retri']['do']:
        doc_retrieval_result_list = first_doc_retrieval(haonan_docretri_object, tokenized_file,
                                                        method=doc_retrieval_method)
        doc_retrieval_file_1 = current_pipeline_dir / f"doc_retr_1_{in_file_stem}.jsonl"
        common.save_jsonl(doc_retrieval_result_list, doc_retrieval_file_1)
        print("First Document Retrieval file saved to:", doc_retrieval_file_1)
    else:
        doc_retrieval_file_1 = steps['s2.1doc_retri']['out_file']
        doc_retrieval_result_list = common.load_jsonl(doc_retrieval_file_1)
        print("Use preprocessed file:", doc_retrieval_file_1)

    if eval_list is not None:
        print("Evaluating 1st Doc Retrieval")
        eval_mode = {'check_doc_id_correct': True, 'standard': False}
        print(c_scorer.fever_score(doc_retrieval_result_list, eval_list, mode=eval_mode, verbose=False))
    # First Document retrieval End.

    # First Sentence Selection.
    print("Step 3. First Sentence Selection")
    if steps['s3.1sen_select']['do']:
        dev_sent_list_1_e0 = simple_nnmodel.pipeline_first_sent_selection(tokenized_file, doc_retrieval_file_1,
                                                                          model_path_dict['sselector'])
        dev_sent_file_1_e0 = current_pipeline_dir / f"dev_sent_score_1_{in_file_stem}.jsonl"
        common.save_jsonl(dev_sent_list_1_e0, dev_sent_file_1_e0)

        # Manual setting, delete it later
        # dev_sent_file_1_e0 = None
        # dev_sent_list_1_e0 = common.load_jsonl("/home/easonnie/projects/FunEver/results/pipeline_r/2018_07_24_11:07:41_r(new_model_v1_2_for_realtest)_scaled_0.05_selector_em/dev_sent_score_1_shared_task_test.jsonl")
        # End

        if steps['s3.1sen_select']['ensemble']:
            print("Ensemble!")
            dev_sent_list_1_e1 = simple_nnmodel.pipeline_first_sent_selection(tokenized_file, doc_retrieval_file_1,
                                                                              model_path_dict['sselector_1'])
            dev_sent_file_1_e1 = current_pipeline_dir / f"dev_sent_score_1_{in_file_stem}_e1.jsonl"
            common.save_jsonl(dev_sent_list_1_e1, dev_sent_file_1_e1)
            # exit(0)
            # dev_sent_list_1_e1 = common.load_jsonl(dev_sent_file_1_e1)

            dev_sent_list_1_e2 = simple_nnmodel.pipeline_first_sent_selection(tokenized_file, doc_retrieval_file_1,
                                                                              model_path_dict['sselector_2'])
            dev_sent_file_1_e2 = current_pipeline_dir / f"dev_sent_score_1_{in_file_stem}_e2.jsonl"
            common.save_jsonl(dev_sent_list_1_e2, dev_sent_file_1_e2)
            # exit(0)
            # dev_sent_list_1_e2 = common.load_jsonl(dev_sent_file_1_e2)

            dev_sent_list_1 = merge_sent_results([dev_sent_list_1_e0, dev_sent_list_1_e1, dev_sent_list_1_e2])
            dev_sent_file_1 = current_pipeline_dir / f"dev_sent_score_1_{in_file_stem}_ensembled.jsonl"
            common.save_jsonl(dev_sent_list_1, dev_sent_file_1)
            # exit(0)
        else:
            dev_sent_list_1 = dev_sent_list_1_e0
            dev_sent_file_1 = dev_sent_file_1_e0
        # Merging two results

        print("First Sentence Selection file saved to:", dev_sent_file_1)

    else:
        dev_sent_file_1 = steps['s3.1sen_select']['out_file']
        dev_sent_list_1 = common.load_jsonl(dev_sent_file_1)
        print("Use preprocessed file:", dev_sent_file_1)

    # exit(0)

    if eval_list is not None:
        print("Evaluating 1st Sentence Selection")
        # sent_select_results_list_1 = simi_sampler.threshold_sampler(tokenized_file, dev_sent_full_list,
        #                                                             sentence_retri_scale_prob, top_n=5)
        # additional_dev_sent_list = common.load_jsonl("/Users/Eason/RA/FunEver/results/sent_retri_nn/2018_07_20_15-17-59_r/dev_sent_2r.jsonl")
        # dev_sent_full_list = dev_sent_full_list + additional_dev_sent_list
        sent_select_results_list_1 = simi_sampler.threshold_sampler_insure_unique(tokenized_file, dev_sent_list_1,
                                                                                  sentence_retri_1_scale_prob,
                                                                                  top_n=sent_retri_1_top_k)
        # sent_select_results_list_1 = simi_sampler.threshold_sampler_insure_unique_merge(sent_select_results_list_1,
        #                                                                                 additional_dev_sent_list,
        #                                                                                 sentence_retri_2_scale_prob,
        #                                                                                 top_n=5, add_n=1)

        eval_mode = {'check_sent_id_correct': True, 'standard': False}
        # for a, b in zip(eval_list, sent_select_results_list_1):
        #     b['predicted_label'] = a['label']
        print(c_scorer.fever_score(sent_select_results_list_1, eval_list, mode=eval_mode, verbose=False))

    print("Step 4. Second Document Retrieval")
    if steps['s4.2doc_retri']['do']:
        dev_sent_list_1 = common.load_jsonl(dev_sent_file_1)
        filtered_dev_instance_1_for_doc2 = simi_sampler.threshold_sampler_insure_unique(tokenized_file,
                                                                                        dev_sent_list_1,
                                                                                        sent_prob_for_2doc,
                                                                                        top_n=sent_topk_for_2doc)
        filtered_dev_instance_1_for_doc2_file = current_pipeline_dir / f"dev_sent_score_1_{in_file_stem}_scaled_for_doc2.jsonl"
        common.save_jsonl(filtered_dev_instance_1_for_doc2, filtered_dev_instance_1_for_doc2_file)

        dev_sent_1_result = simi_sampler.threshold_sampler_insure_unique(doc_retrieval_file_1,  # Remember this name
                                                                         dev_sent_list_1,
                                                                         sentence_retri_1_scale_prob,
                                                                         top_n=sent_topk_for_2doc)

        dev_doc2_list = second_doc_retrieval(haonan_docretri_object, filtered_dev_instance_1_for_doc2_file,
                                             dev_sent_1_result)

        dev_doc2_file = current_pipeline_dir / f"doc_retr_2_{in_file_stem}.jsonl"
        common.save_jsonl(dev_doc2_list, dev_doc2_file)
        print("Second Document Retrieval File saved to:", dev_doc2_file)
    else:
        dev_doc2_file = steps['s4.2doc_retri']['out_file']
        # dev_doc2_list = common.load_jsonl(dev_doc2_file)
        print("Use preprocessed file:", dev_doc2_file)

    print("Step 5. Second Sentence Selection")
    if steps['s5.2sen_select']['do']:
        dev_sent_2_list = get_score_multihop(tokenized_file,
                                             dev_doc2_file,
                                             model_path=model_path_dict['sselector'])

        dev_sent_file_2 = current_pipeline_dir / f"dev_sent_score_2_{in_file_stem}.jsonl"
        common.save_jsonl(dev_sent_2_list, dev_sent_file_2)
        print("First Sentence Selection file saved to:", dev_sent_file_2)
    else:
        dev_sent_file_2 = steps['s5.2sen_select']['out_file']

    if eval_list is not None:
        print("Evaluating 1st Sentence Selection")
        dev_sent_list_1 = common.load_jsonl(dev_sent_file_1)
        dev_sent_list_2 = common.load_jsonl(dev_sent_file_2)
        sent_select_results_list_1 = simi_sampler.threshold_sampler_insure_unique(tokenized_file, dev_sent_list_1,
                                                                                  sentence_retri_1_scale_prob, top_n=5)
        sent_select_results_list_1 = simi_sampler.threshold_sampler_insure_unique_merge(sent_select_results_list_1,
                                                                                        dev_sent_list_2,
                                                                                        sentence_retri_2_scale_prob,
                                                                                        top_n=5,
                                                                                        add_n=sent_retri_2_top_k)
        eval_mode = {'check_sent_id_correct': True, 'standard': False}
        # for a, b in zip(eval_list, sent_select_results_list_1):
        #     b['predicted_label'] = a['label']
        print(c_scorer.fever_score(sent_select_results_list_1, eval_list, mode=eval_mode, verbose=False))

    # print("Step 6. NLI")
    # if steps['s6.nli']['do']:
    #     dev_sent_list_1 = common.load_jsonl(dev_sent_file_1)
    #     dev_sent_list_2 = common.load_jsonl(dev_sent_file_2)
    #     sentence_retri_1_scale_prob = 0.05
    #     print("Threshold:", sentence_retri_1_scale_prob)
    #     sent_select_results_list_1 = simi_sampler.threshold_sampler_insure_unique(tokenized_file, dev_sent_list_1,
    #                                                                               sentence_retri_1_scale_prob, top_n=5)
    #     # sent_select_results_list_2 = simi_sampler.threshold_sampler_insure_unique_merge(sent_select_results_list_1,
    #     #                                                                                 dev_sent_list_2,
    #     #                                                                                 sentence_retri_2_scale_prob,
    #     #                                                                                 top_n=5,
    #     #                                                                                 add_n=sent_retri_2_top_k)
    #     nli_results = nli.mesim_wn_simi_v1_2.pipeline_nli_run(tokenized_file,
    #                                                           sent_select_results_list_1,
    #                                                           [dev_sent_file_1, dev_sent_file_2],
    #                                                           model_path_dict['nli'],
    #                                                           with_logits=True,
    #                                                           with_probs=True)
    #
    #     nli_results_file = current_pipeline_dir / f"nli_r_{in_file_stem}.jsonl"
    #     common.save_jsonl(nli_results, nli_results_file)
    # else:
    #     nli_results_file = steps['s6.nli']['out_file']
    #     nli_results = common.load_jsonl(nli_results_file)

    # Ensemble code
    # dev_sent_list_1 = common.load_jsonl(dev_sent_file_1)
    # dev_sent_list_2 = common.load_jsonl(dev_sent_file_2)
    # sentence_retri_1_scale_prob = 0.05
    # print("NLI sentence threshold:", sentence_retri_1_scale_prob)
    # sent_select_results_list_1 = simi_sampler.threshold_sampler_insure_unique(tokenized_file, dev_sent_list_1,
    #                                                                           sentence_retri_1_scale_prob, top_n=5)
    #
    # # sent_select_results_list_2 = simi_sampler.threshold_sampler_insure_unique_merge(sent_select_results_list_1,
    # #                                                                                 dev_sent_list_2,
    # #                                                                                 sentence_retri_2_scale_prob,
    # #                                                                                 top_n=5,
    # #                                                                                 add_n=sent_retri_2_top_k)
    # # nli_results = nli.mesim_wn_simi_v1_2.pipeline_nli_run(tokenized_file,
    # #                                                       sent_select_results_list_1,
    # #                                                       [dev_sent_file_1, dev_sent_file_2],
    # #                                                       model_path_dict['nli'], with_probs=True, with_logits=True)
    #
    # # nli_results = nli.mesim_wn_simi_v1_2.pipeline_nli_run_bigger(tokenized_file,
    # #                                                       sent_select_results_list_1,
    # #                                                       [dev_sent_file_1, dev_sent_file_2],
    # #                                                       model_path_dict['nli_2'],
    # #                                                              with_probs=True,
    # #                                                              with_logits=True)
    #
    # nli_results = nli.mesim_wn_simi_v1_2.pipeline_nli_run_bigger(tokenized_file,
    #                                                       sent_select_results_list_1,
    #                                                       [dev_sent_file_1, dev_sent_file_2],
    #                                                       model_path_dict['nli_4'],
    #                                                       with_probs=True,
    #                                                       with_logits=True)
    #
    # nli_results_file = current_pipeline_dir / f"nli_r_{in_file_stem}_withlb_e4.jsonl"
    # common.save_jsonl(nli_results, nli_results_file)
    # Ensemble code end
    # exit(0)

    nli_r_e0 = common.load_jsonl(
        current_pipeline_dir / "nli_r_shared_task_test_withlb_e0.jsonl")
    nli_r_e1 = common.load_jsonl(
        current_pipeline_dir / "nli_r_shared_task_test_withlb_e1.jsonl")
    nli_r_e2 = common.load_jsonl(
        current_pipeline_dir / "nli_r_shared_task_test_withlb_e2.jsonl")
    nli_r_e3 = common.load_jsonl(
        current_pipeline_dir / "nli_r_shared_task_test_withlb_e3.jsonl")
    nli_r_e4 = common.load_jsonl(
        current_pipeline_dir / "nli_r_shared_task_test_withlb_e4.jsonl")

    nli_results = merge_nli_results([nli_r_e0, nli_r_e1, nli_r_e2, nli_r_e3, nli_r_e4])

    print("Post Processing enhancement")
    delete_unused_evidence(nli_results)
    print("Deleting Useless Evidence")

    dev_sent_list_1 = common.load_jsonl(dev_sent_file_1)
    dev_sent_list_2 = common.load_jsonl(dev_sent_file_2)

    print("Appending 1 of second Evidence")
    nli_results = simi_sampler.threshold_sampler_insure_unique_merge(nli_results,
                                                                     dev_sent_list_2,
                                                                     sentence_retri_2_scale_prob,
                                                                     top_n=5,
                                                                     add_n=sent_retri_2_top_k)
    delete_unused_evidence(nli_results)

    # High tolerance enhancement!
    print("Final High Tolerance Enhancement")
    print("Appending all of first Evidence")
    nli_results = simi_sampler.threshold_sampler_insure_unique_merge(nli_results,
                                                                     dev_sent_list_1,
                                                                     enhance_retri_1_scale_prob,
                                                                     top_n=100,
                                                                     add_n=100)
    delete_unused_evidence(nli_results)

    if build_submission:
        output_file = current_pipeline_dir / "predictions.jsonl"
        build_submission_file(nli_results, output_file)


def pipeline_tokenize(in_file, out_file):
    tokenized_claim(in_file, out_file)


def first_doc_retrieval(retri_object, in_file, method='pageview'):
    # doc_exp = DocRetrievalExperiment()
    init_haonan_docretri_object(retri_object, method=method)
    d_list = common.load_jsonl(in_file)
    retri_object.instance.sample_answer_with_priority(d_list)
    return d_list


def second_doc_retrieval(retri_object, upstream_sent_file, additiona_d_list):
    # doc_exp = DocRetrievalExperimentSpiral()
    init_haonan_docretri_object(retri_object)
    # additiona_d_list = common.load_jsonl(in_file)
    retri_object.instance.feed_sent_file(upstream_sent_file)
    retri_object.instance.find_sent_link_with_priority(additiona_d_list, predict=True)
    return additiona_d_list


def nli_f(upstream_dev_file, additional_sent_file_list):
    pass


def append_hidden_label(d_list):
    for item in d_list:
        item['label'] = 'hidden'
    return d_list


def nicer(d_list):
    new_list = copy.deepcopy(d_list)
    print("DON'T Do this unless you know what you are doing!!!")
    print("DON'T Do this unless you know what you are doing!!!")
    print("DON'T Do this unless you know what you are doing!!!")
    print("DON'T Do this unless you know what you are doing!!!")
    print("DON'T Do this unless you know what you are doing!!!")
    top_k = 10000
    for item in new_list[:top_k]:
        item['predicted_label'] = 'NOT ENOUGH INFO'
        item['predicted_evidence'] = []
    print("DON'T Do this unless you know what you are doing!!!")
    print("DON'T Do this unless you know what you are doing!!!")
    print("DON'T Do this unless you know what you are doing!!!")
    print("DON'T Do this unless you know what you are doing!!!")
    print("DON'T Do this unless you know what you are doing!!!")
    return new_list


def build_submission_file(d_list, filename):
    with open(filename, encoding='utf-8', mode='w') as out_f:
        for item in d_list:
            instance_item = dict()
            instance_item['id'] = item['id']
            instance_item['predicted_label'] = item['predicted_label']
            instance_item['predicted_evidence'] = item['predicted_evidence']
            out_f.write(json.dumps(instance_item) + "\n")


# New method added
def delete_unused_evidence(d_list):
    for item in d_list:
        if item['predicted_label'] == 'NOT ENOUGH INFO':
            item['predicted_evidence'] = []


def post_process():
    from pathlib import Path
    input_file = '/home/easonnie/projects/FunEver/results/pipeline_r/2018_07_24_11:07:41_r(new_model_v1_2_for_realtest)_scaled_0.05_withlb/balance.jsonl'
    nli_results = common.load_jsonl(input_file)
    print("Post Processing enhancement")
    delete_unused_evidence(nli_results)
    print("Deleting Useless Evidence")

    current_pipeline_dir = Path(
        '/home/easonnie/projects/FunEver/results/pipeline_r/2018_07_24_11:07:41_r(new_model_v1_2_for_realtest)_scaled_0.05_withlb')
    dev_sent_file_1 = current_pipeline_dir / "dev_sent_score_1_shared_task_test.jsonl"
    dev_sent_file_2 = current_pipeline_dir / "dev_sent_score_2_shared_task_test.jsonl"

    dev_sent_list_1 = common.load_jsonl(dev_sent_file_1)
    dev_sent_list_2 = common.load_jsonl(dev_sent_file_2)

    print("Appending 1 of second Evidence")
    nli_results = simi_sampler.threshold_sampler_insure_unique_merge(nli_results,
                                                                     dev_sent_list_2,
                                                                     0.9,
                                                                     top_n=5,
                                                                     add_n=1)
    delete_unused_evidence(nli_results)

    # High tolerance enhancement!
    print("Final High Tolerance Enhancement")
    print("Appending all of first Evidence")
    nli_results = simi_sampler.threshold_sampler_insure_unique_merge(nli_results,
                                                                     dev_sent_list_1,
                                                                     -1,
                                                                     top_n=5,
                                                                     add_n=100)
    delete_unused_evidence(nli_results)

    # if build_submission:
    output_file = current_pipeline_dir / "predictions.jsonl"
    build_submission_file(nli_results, output_file)


if __name__ == '__main__':
    # p_steps = {
    #     's1.tokenizing': {
    #         'do': False,
    #         'out_file': config.RESULT_PATH / "pipeline_r/2018_07_23_15:10:56_r/t_shared_task_dev_public.jsonl"
    #     },
    #     's2.1doc_retri': {
    #         'do': False,
    #         'out_file': config.RESULT_PATH / "pipeline_r/2018_07_23_15:10:56_r/doc_retr_1_shared_task_dev_public.jsonl"
    #     },
    #     's3.1sen_select': {
    #         'do': False,
    #         'out_file': config.RESULT_PATH / "pipeline_r/2018_07_23_15:10:56_r/dev_sent_score_1_shared_task_dev_public.jsonl"
    #     },
    #     's4.2doc_retri': {
    #         'do': False,
    #         'out_file': config.RESULT_PATH / "pipeline_r/2018_07_23_15:10:56_r/doc_retr_2_shared_task_dev_public.jsonl"
    #     },
    #     's5.2sen_select': {
    #         'do': False,
    #         'out_file': config.RESULT_PATH / "pipeline_r/2018_07_23_15:10:56_r/dev_sent_score_2_shared_task_dev_public.jsonl"
    #     },
    #     's6.nli': {
    #         'do': True,
    #     }
    # }

    # p_steps = {
    #     's1.tokenizing': {
    #         'do': False,
    #         'out_file': config.RESULT_PATH / "pipeline_r/2018_07_22_16:32:52_r/t_shared_task_dev_public.jsonl"
    #     },
    #     's2.1doc_retri': {
    #         'do': False,
    #         'out_file': config.RESULT_PATH / "pipeline_r/2018_07_22_16:32:52_r/doc_retr_1_shared_task_dev_public.jsonl"
    #     },
    #     's3.1sen_select': {
    #         'do': False,
    #         'out_file': config.RESULT_PATH / "sent_retri_nn/2018_07_20_15-17-59_r/dev_sent.jsonl"
    #     },
    #     's4.2doc_retri': {
    #         'do': True,
    #     },
    #     's5.2sen_select': {
    #         'do': True,
    #     },
    #     's6.nli': {
    #         'do': True,
    #     }
    # }

    p_steps = {
        's1.tokenizing': {
            'do': False,
            'out_file': config.RESULT_PATH / "pipeline_r/2018_07_24_11:07:41_r(new_model_v1_2_for_realtest)_scaled_005_selector_and_nli_em/t_shared_task_test.jsonl"
        },
        's2.1doc_retri': {
            'do': False,
            'out_file': config.RESULT_PATH / "pipeline_r/2018_07_24_11:07:41_r(new_model_v1_2_for_realtest)_scaled_005_selector_and_nli_em/doc_retr_1_shared_task_test.jsonl"
        },
        's3.1sen_select': {
            'do': False,
            'out_file': config.RESULT_PATH / "pipeline_r/2018_07_24_11:07:41_r(new_model_v1_2_for_realtest)_scaled_005_selector_and_nli_em/dev_sent_score_1_shared_task_test_ensembled.jsonl",
            'ensemble': True,
        },
        's4.2doc_retri': {
            'do': False,
            'out_file': config.RESULT_PATH / "pipeline_r/2018_07_24_11:07:41_r(new_model_v1_2_for_realtest)_scaled_005_selector_and_nli_em/doc_retr_2_shared_task_test.jsonl"
        },
        's5.2sen_select': {
            'do': False,
            'out_file': config.RESULT_PATH / "pipeline_r/2018_07_24_11:07:41_r(new_model_v1_2_for_realtest)_scaled_005_selector_and_nli_em/dev_sent_score_2_shared_task_test.jsonl"
        },
        's6.nli': {
            'do': True,
            'out_file': "hidden"
        }
    }

    pipeline(config.DATA_ROOT / "fever/shared_task_test.jsonl",
             eval_file=None,
             model_path_dict=default_model_path_dict,
             steps=p_steps)

    # post_process()

    # pipeline(config.DATA_ROOT / "fever/shared_task_dev_public.jsonl",
    #          eval_file=config.T_FEVER_DEV_JSONL,
    #          model_path_dict=default_model_path_dict,
    #          steps=default_steps)
