import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / 'docker'))

import check_fpga_config  # noqa: E402


def make_conf(delay_table_folder='{data_folder}/adc_delay_tables', data_folder='/data'):
    """Return a loaded-config dict with the fields the check reads."""
    adc_delay_params = {'source': 'default'}
    if delay_table_folder is not None:
        adc_delay_params['delay_table_folder'] = delay_table_folder
    return {
        'corr_name': 'TEST',
        'run_name': '{isotime}_{corr_name}',
        'data_folder': data_folder,
        'run_folder': '{data_folder}/{run_name}',
        'fpga': {'adc_delay_params': adc_delay_params},
    }


def test_docker_example_config_passes():
    assert check_fpga_config.main([str(REPO_ROOT / 'config' / 'docker_example.yaml'), 'EXAMPLE']) == 0


@pytest.mark.parametrize('folder', [
    '{data_folder}/adc_delay_tables',
    '%(data_folder)s/adc_delay_tables',
    '{run_folder}/adc_delay_tables',
    '/data/adc_delay_tables',
    '/data',
])
def test_folders_on_the_data_mount_pass(folder):
    assert check_fpga_config.delay_table_folder_problem(make_conf(folder)) is None


def test_missing_adc_delay_params_is_refused():
    conf = make_conf()
    del conf['fpga']['adc_delay_params']
    assert 'is not set' in check_fpga_config.delay_table_folder_problem(conf)


def test_missing_delay_table_folder_is_refused():
    assert 'is not set' in check_fpga_config.delay_table_folder_problem(make_conf(None))


@pytest.mark.parametrize('folder, data_folder, resolved', [
    ('/tmp/delays', '/data', '/tmp/delays'),
    ('/data2/delays', '/data', '/data2/delays'),        # shares the prefix, not the mount
    ('/data/../tmp/delays', '/data', '/tmp/delays'),
    ('{data_folder}/delays', '/srv/data', '/srv/data/delays'),
])
def test_folders_off_the_data_mount_are_refused(folder, data_folder, resolved):
    problem = check_fpga_config.delay_table_folder_problem(make_conf(folder, data_folder))
    assert f"resolves to '{resolved}'" in problem


def test_home_relative_data_folder_is_refused():
    problem = check_fpga_config.delay_table_folder_problem(make_conf(data_folder='~/data'))
    assert 'outside the /data mount' in problem


def test_main_refuses_a_config_without_the_folder(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text(
        "TEST:\n"
        "    corr_name: TEST\n"
        "    run_name: '{isotime}_{corr_name}'\n"
        "    data_folder: '/data'\n"
        "    run_folder: '{data_folder}/{run_name}'\n"
        "    fpga:\n"
        "        adc_delay_params:\n"
        "            source: 'default'\n")
    assert check_fpga_config.main([str(config), 'TEST']) == 1
