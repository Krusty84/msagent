#!/usr/bin/env python3
"""
_data_io.py 单元测试
"""

import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "skills", "compare-result-analyzer", "scripts"))

import _data_io as dio
import openpyxl


STAT_HEADERS = [
    'NPU Name', 'Bench Name', 'NormRelativeErr', 'MeanRelativeErr',
    'MaxRelativeErr', 'MinRelativeErr', 'Max diff', 'Mean diff',
    'L2norm diff', 'Bench l2norm', 'NPU l2norm', 'Result', 'NPU Dtype',
    'NPU Tensor Shape', 'Bench Dtype', 'Bench Tensor Shape',
    'Requires_grad Consistent',
]


def _write_csv(path, headers, rows):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for r in rows:
            writer.writerow({h: r.get(h, '') for h in headers})


def _write_xlsx(path, headers, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append([r.get(h, '') for h in headers])
    wb.save(path)
    wb.close()


class TestDetectDataMode(unittest.TestCase):
    def test_stat_mode(self):
        self.assertEqual(dio.detect_data_mode(['Max diff', 'NPU Name']), 'stat')


class TestLoadRows(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.csv_path = os.path.join(self.tmpdir, 'stat.csv')
        self.xlsx_path = os.path.join(self.tmpdir, 'stat.xlsx')
        rows = [
            {h: '' for h in STAT_HEADERS},
            {h: '' for h in STAT_HEADERS},
        ]
        rows[0]['NPU Name'] = 'Module.A.forward.0.input.0'
        rows[0]['NormRelativeErr'] = '0.5'
        rows[0]['Bench Name'] = 'bench0'
        rows[1]['NPU Name'] = 'Module.A.forward.0.output.0'
        rows[1]['NormRelativeErr'] = '0.1'
        rows[1]['Bench Name'] = 'bench1'
        _write_csv(self.csv_path, STAT_HEADERS, rows)
        _write_xlsx(self.xlsx_path, STAT_HEADERS, rows)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_csv(self):
        rows = dio.load_rows(self.csv_path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['NPU Name'], 'Module.A.forward.0.input.0')
        self.assertEqual(rows[0]['RowIndex'], 2)
        self.assertEqual(rows[1]['RowIndex'], 3)

    def test_load_xlsx(self):
        rows = dio.load_rows(self.xlsx_path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['NPU Name'], 'Module.A.forward.0.input.0')
        self.assertEqual(rows[0]['RowIndex'], 2)
        self.assertEqual(rows[1]['RowIndex'], 3)


class TestFilterNaRows(unittest.TestCase):
    def test_filters_na_rows(self):
        rows = [
            {'NPU Name': 'op1', 'Bench Name': 'b1'},
            {'NPU Name': '', 'Bench Name': 'b2'},
            {'NPU Name': 'N/A', 'Bench Name': 'b3'},
            {'NPU Name': 'op4', 'Bench Name': ''},
            {'NPU Name': 'op5', 'Bench Name': 'N/A'},
            {'NPU Name': '  ', 'Bench Name': '  '},
        ]
        filtered = dio.filter_na_rows(rows)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]['NPU Name'], 'op1')


class TestCollectStatNodes(unittest.TestCase):
    def test_collects_nodes_and_mean_bias(self):
        rows = [
            {'NPU Name': 'op1', 'Bench Name': 'b1', 'NormRelativeErr': '0.5',
             'MeanRelativeErr': '0.1', 'MaxRelativeErr': '0.9', 'MinRelativeErr': '0.01',
             'Max diff': '0.2', 'Mean diff': '0.05', 'L2norm diff': '0.1',
             'Bench l2norm': '1.0', 'NPU l2norm': '0.95', 'Result': 'pass',
             'NPU Dtype': 'torch.float32', 'NPU Tensor Shape': '(2,)', 'RowIndex': 2},
            {'NPU Name': 'op2', 'Bench Name': 'b2', 'NormRelativeErr': '',
             'MeanRelativeErr': '', 'MaxRelativeErr': '', 'Mean diff': '',
             'Bench l2norm': '', 'RowIndex': 3},
        ]
        nodes = dio.collect_stat_nodes(rows)
        # second row has nre/mean_re/max_re all None -> skipped
        self.assertEqual(len(nodes), 1)
        n = nodes[0]
        self.assertEqual(n['name'], 'op1')
        self.assertEqual(n['nre'], 0.5)
        self.assertEqual(n['idx'], 2)
        self.assertAlmostEqual(n['mean_bias'], 0.05)  # 0.05 / 1.0


class TestNaSummary(unittest.TestCase):
    def test_summary_values(self):
        rows = [{'NPU Name': 'a', 'Bench Name': 'b'}] * 10
        filtered = rows[:7]
        summary = dio.na_summary(rows, filtered, threshold=0.05,
                                 csv_path='/tmp/x.csv', analysis_range=300)
        self.assertEqual(summary['total_rows'], 10)
        self.assertEqual(summary['valid_rows'], 7)
        self.assertEqual(summary['na_count'], 3)
        self.assertEqual(summary['threshold'], 0.05)
        self.assertEqual(summary['file_path'], os.path.abspath('/tmp/x.csv'))
        self.assertEqual(summary['analysis_range'], {'upstream': 300, 'downstream': 300})
        self.assertIn('analysis_time', summary)
        self.assertAlmostEqual(summary['na_ratio'], 3 / 10)


class TestMetaErrors(unittest.TestCase):
    def test_dtype_shape_and_grad_mismatch(self):
        rows = [
            {'NPU Name': 'op1', 'NPU Dtype': 'torch.float32', 'Bench Dtype': 'torch.float16',
             'NPU Tensor Shape': '(2,)', 'Bench Tensor Shape': '(3,)',
             'Requires_grad Consistent': 'false', 'RowIndex': 2},
            {'NPU Name': 'op2', 'NPU Dtype': 'torch.float32', 'Bench Dtype': 'torch.float32',
             'NPU Tensor Shape': '(2,)', 'Bench Tensor Shape': '(2,)',
             'Requires_grad Consistent': 'true', 'RowIndex': 3},
        ]
        result = dio.meta_errors(rows)
        self.assertEqual(len(result['dtype_mismatch']), 1)
        self.assertEqual(result['dtype_mismatch'][0]['npu_dtype'], 'torch.float32')
        self.assertEqual(len(result['shape_mismatch']), 1)
        self.assertEqual(len(result['requires_grad_mismatch']), 1)
        # 1/2 = 0.5 -> not > 0.5 -> 'warning'
        self.assertEqual(result['shape_mismatch_level'], 'warning')
        self.assertEqual(result['shape_mismatch_ratio'], 0.5)

    def test_no_mismatch(self):
        rows = [{
            'NPU Name': 'op1', 'NPU Dtype': 'torch.float32', 'Bench Dtype': 'torch.float32',
            'NPU Tensor Shape': '(2,)', 'Bench Tensor Shape': '(2,)',
            'Requires_grad Consistent': 'true', 'RowIndex': 2,
        }]
        result = dio.meta_errors(rows)
        self.assertEqual(len(result['dtype_mismatch']), 0)
        self.assertEqual(len(result['shape_mismatch']), 0)
        self.assertEqual(result['shape_mismatch_level'], 'normal')


class TestBuildRowIndex(unittest.TestCase):
    def setUp(self):
        dio._row_index_cache.clear()
        dio._cache_source_id = None

    def tearDown(self):
        dio._row_index_cache.clear()
        dio._cache_source_id = None

    def test_builds_cache(self):
        rows = [
            {'NPU Name': 'Module.A.forward.0.input.0'},
            {'NPU Name': 'Module.A.backward.0.output.0'},
            {'NPU Name': 'Module.A.forward.0.output.0'},
            {'NPU Name': ''},  # empty name skipped
        ]
        dio._build_row_index(rows)
        self.assertEqual(dio._cache_source_id, id(rows))
        fwd = dio._row_index_cache.get(('Module.A.forward.0', 'forward'))
        self.assertEqual(len(fwd), 2)
        bwd = dio._row_index_cache.get(('Module.A.backward.0', 'backward'))
        self.assertEqual(len(bwd), 1)


class TestGetInputNresForOp(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        dio._row_index_cache.clear()
        dio._cache_source_id = None
        self.rows = [
            {'NPU Name': 'Module.A.forward.0.input.0', 'NormRelativeErr': '0.1',
             'MeanRelativeErr': '0.01', 'MaxRelativeErr': '0.2',
             'Mean diff': '0.02', 'Bench l2norm': '1.0',
             'NPU Tensor Shape': '(2,)', 'NPU Dtype': 'torch.float32'},
            {'NPU Name': 'Module.A.forward.0.output.0', 'NormRelativeErr': '0.9',
             'Mean diff': '', 'Bench l2norm': ''},
            {'NPU Name': 'Module.A.forward.0.kwargs.0', 'NormRelativeErr': '0.3',
             'Mean diff': '0.04', 'Bench l2norm': '2.0',
             'NPU Tensor Shape': '(1,)', 'NPU Dtype': 'torch.float16'},
        ]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        dio._row_index_cache.clear()
        dio._cache_source_id = None

    def test_with_cache(self):
        dio._build_row_index(self.rows)
        result = dio.get_input_nres_for_op(self.rows, 'Module.A.forward.0', 'forward')
        # only input.0 and kwargs.0 qualify (output excluded)
        keys = [r[0] for r in result]
        self.assertEqual(keys, ['input.0', 'kwargs.0'])
        # check tuple structure: (param_suffix, nre, mean_re, max_re, shape, dtype, mean_bias)
        inp = next(r for r in result if r[0] == 'input.0')
        self.assertAlmostEqual(inp[1], 0.1)  # nre
        self.assertEqual(inp[5], 'torch.float32')  # dtype
        self.assertAlmostEqual(inp[6], 0.02)  # mean_bias = 0.02/1.0

    def test_fallback_full_scan(self):
        # cache empty -> full scan path
        result = dio.get_input_nres_for_op(self.rows, 'Module.A.forward.0', 'forward')
        keys = [r[0] for r in result]
        self.assertEqual(keys, ['input.0', 'kwargs.0'])


class TestOutputNodesDetail(unittest.TestCase):
    def test_filters_output_nodes(self):
        nodes = [
            {'idx': 2, 'name': 'Module.A.forward.0.output.0', 'nre': 0.5,
             'mean_re': 0.1, 'max_re': 0.9, 'min_re': 0.01,
             'dtype': 'torch.float32', 'shape': '(2,)', 'result': 'pass'},
            {'idx': 3, 'name': 'Module.A.parameters_grad.0.weight', 'nre': 0.7,
             'mean_re': None, 'max_re': None, 'min_re': None,
             'dtype': 'torch.float32', 'shape': '(3,)', 'result': 'fail'},
            {'idx': 4, 'name': 'Module.A.forward.0.input.0', 'nre': 0.2,
             'mean_re': None, 'max_re': None, 'min_re': None,
             'dtype': '', 'shape': '', 'result': ''},
        ]
        output_nodes, result = dio.output_nodes_detail(nodes)
        self.assertEqual(len(output_nodes), 2)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['row_index'], 2)
        self.assertEqual(result[1]['row_index'], 3)
        self.assertEqual(result[0]['name'], 'Module.A.forward.0.output.0')


class TestAllBadNodesDetail(unittest.TestCase):
    def test_filters_by_threshold_and_noise(self):
        nodes = [
            {'idx': 5, 'name': 'op_high', 'nre': 0.8, 'is_noise': False,
             'mean_re': None, 'max_re': None, 'min_re': None,
             'dtype': '', 'shape': '', 'result': ''},
            {'idx': 2, 'name': 'op_low', 'nre': 0.1, 'is_noise': False,
             'mean_re': None, 'max_re': None, 'min_re': None,
             'dtype': '', 'shape': '', 'result': ''},
            {'idx': 9, 'name': 'op_noise', 'nre': 0.9, 'is_noise': True,
             'mean_re': None, 'max_re': None, 'min_re': None,
             'dtype': '', 'shape': '', 'result': ''},
            {'idx': 7, 'name': 'op_none_nre', 'nre': None, 'is_noise': False,
             'mean_re': None, 'max_re': None, 'min_re': None,
             'dtype': '', 'shape': '', 'result': ''},
        ]
        bad, result = dio.all_bad_nodes_detail(nodes, threshold=0.5)
        # only op_high (0.8 >= 0.5, not noise)
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]['name'], 'op_high')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['row_index'], 5)
        # sorted by idx
        self.assertEqual(result[0]['row_index'], 5)

    def test_sorted_by_idx(self):
        nodes = [
            {'idx': 10, 'name': 'op_b', 'nre': 0.9, 'is_noise': False,
             'mean_re': None, 'max_re': None, 'min_re': None,
             'dtype': '', 'shape': '', 'result': ''},
            {'idx': 3, 'name': 'op_a', 'nre': 0.9, 'is_noise': False,
             'mean_re': None, 'max_re': None, 'min_re': None,
             'dtype': '', 'shape': '', 'result': ''},
        ]
        bad, result = dio.all_bad_nodes_detail(nodes, threshold=0.5)
        self.assertEqual([r['row_index'] for r in result], [3, 10])


if __name__ == '__main__':
    unittest.main()
