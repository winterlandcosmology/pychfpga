from random import randint
from time import sleep
from wtl.pytest_xreport import xr, get_xr_from
from pytest_html.extras import html

import pytest
import numpy as np
import matplotlib.pyplot as plt


def test_dummy(xr):

    plt.figure(1)

    ideal_ramp = (np.arange(2048) - 128).astype(np.int8)

    print('allo. This is a great report.\n')
    plt.plot(ideal_ramp)
    xr.insert_plot('Ideal ramp')


def test_fig(xr, extras):
    with xr.fig('Ideal ramp'):
        plt.plot(range(10), '.-')

def test_extras(xr, extras):
    extras.append(html('<div> SuperHTML</div>'))
    print('extra was added')


# @pytest.fixture(scope='session', autouse=True)
def test_session(xr_session): # can't use xr here because it's a function-scope fixture
    print(f'Session in progress')
    xr = xr_session
    xr.data.a=1
    print(f'{xr.config=}')
    print(f'{xr.data=}')

class TestDummy:


    @pytest.fixture(scope='session', autouse=True)
    def session_fixture(self, pytestconfig): # can't use xr here because it's a function-scope fixture
        print(f'Session in progress')
        xr = get_xr_from(pytestconfig)
        xr.data.a=1
        print(f'{xr.config=}')
        print(f'{xr.data=}')

    def pytest_generate_tests(self, metafunc):
        print(f'Calling pytest_generate_tests')
        xr = get_xr_from(metafunc)
        xr.data.x=1
        print(f'{xr.config=}')
        print(f'{xr.data=}')

    def test_dummy(self, xr):
        print(f'dummier than ever')
        xr.data.y=2
        print(f'{xr.config=}')
        print(f'{xr.data=}')

    def test_dummy2(self, xr):
        print(f'Dummy2 is here')
        xr.data.z=3
        print(f'{xr.config=}')
        print(f'{xr.data=}')
        print(f'{xr.params=}')
