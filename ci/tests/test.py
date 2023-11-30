
import argparse
import copy
import http
import logging
import os
import requests.adapters
import ssl
import subprocess
import traceback
import urllib3

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

urllib3.disable_warnings()


def init_logging(loglevel=None):
    numeric_level = getattr(logging, loglevel.upper(), None)

    if not isinstance(numeric_level, int):
        raise ValueError('Invalid log level: %s' % loglevel)

    logging.basicConfig()
    logger = logging.getLogger().setLevel(numeric_level)

    # something not right with urllib3 and propagation?
    # prints duplicates, and prints a few debug lines when it shouldn't (eg NOTSET)
    # unless overridden to INFO here (but maybe that's causing the dupes?)

    urllib3_log = logging.getLogger("urllib3.connectionpool")
    urllib3_log.setLevel(logging.INFO)
    urllib3_log.propagate = True

    requests_log = logging.getLogger("requests.packages.urllib3")
    requests_log.setLevel(numeric_level)
    requests_log.propagate = True

    logger.debug("numeric log level: {}".format(numeric_level))
    http.client.HTTPConnection.debuglevel = numeric_level
    return logger


class CITestsHandler:

    dashboard_url = os.getenv('SDK_DASHBOARD_URL') or os.getenv('TESTING_HOST')
    record = {
        'id': os.getenv('SITE_ID'),
        'key': os.getenv('SDK_DASHBOARD_TOKEN'),  # site_token
        'data': {
            'run_id': os.getenv('CI_PIPELINE_ID') or os.getenv('RANDOM'),
            'branch': os.getenv('CI_COMMIT_BRANCH'),
            'test_name': '',
            'test_start_time': '',
            'test_end_time': '',
            'module': '',
            'function': '',
            'results': {},
            'extras': {}
        }
    }

    def __init__(self):
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ctx.options |= 0x4  # OP_LEGACY_SERVER_CONNECT
        ctx.check_hostname = False
        self._session = requests.session()
        self._session.mount('https://', TransportAdapter(ctx))

    def run(self, start: Optional[bool] = False, end: Optional[bool] = False,
            command: Optional[str] = None,
            loglevel: Optional[str] = None, **kwargs: Any) -> None:
        assert ismuex(start, end, command), 'Arguments are mutually exclusive'

        record = copy.deepcopy(self.record)

        if start:
            record['data'].update({
                'test_name': 'Set Environment',
                'test_start_time': str(datetime.now()),
                'test_end_time': str(datetime.now()),
                'module': '_conftest',
                'function': '_discover_environment',
                'extras': {
                    'start_time': str(datetime.now()),  # run_start_time
                    'git_branch': record['data']['branch'],
                    'config': {
                        'im_number': os.getenv('IM_NUMBER'),
                        'maintainer_email': os.getenv('MAINTAINER')
                    }
                }
            })

        elif end:
            record['data'].update({
                'test_name': 'End Test Series',
                'test_start_time': str(datetime.now()),
                'test_end_time': str(datetime.now()),
                'module': '_conftest',
                'function': '_end'
            })

        elif command:
            tests_group = os.getenv('TESTS_GROUP', '').lower()
            name = kwargs.get('name') or command.split()[0]

            start_time = str(datetime.now())
            results, out = self.execute_test(command)
            record['data'].update({
                'test_name': name,
                'test_start_time': start_time,
                'test_end_time': str(datetime.now()),
                'module': tests_group,
                'function': 'main',
                'results': results,
                'extras': {
                    'tests_group': tests_group
                }
            })

            if kwargs.get('stdout'):
                print(out)

            print('### %s: %s' % (name, results['call']['status']))

        else:
            raise RuntimeError('No viable option called, exiting...')

        response = self._session.post(self.dashboard_url, json=record, verify=False)
        logger.debug('response body({})'.format(response.text))

    @staticmethod
    def execute_test(command: str) -> Tuple[Dict, str]:
        results = {'setup': {'passed': True,
                             'status': 'passed',
                             'exception': None,
                             'report': ''},
                   'call':  {'passed': False,
                             'status': '',  # passed, failed
                             'exception': None,
                             'report': ''}}

        try:
            out = subprocess.check_output(command, shell=True,
                                          stderr=subprocess.STDOUT,
                                          timeout=300)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            out = e.output.decode('utf-8') if e.output else ''
            status = 'failed'
            exception = str(repr(e))
        except KeyboardInterrupt as e:
            out = traceback.format_exc()
            status = 'failed'
            exception = str(repr(e))
        else:
            out = out.decode('utf-8') if out else ''
            status = 'passed'
            exception = None

        passed = bool(status == 'passed')
        results['call'].update({'passed': passed,
                                'status': status,
                                'exception': exception,
                                'report': out if not passed else ''})

        return results, out


def ismuex(*a):
    return not bool(sum(map(
        lambda v: bool(v if isinstance(v, bool) else v is not None), a)) > 1)


def get_args():
    """
    Get arguments.
    :return: Arguments namespace.
    :rtype: _AttributeHolder
    """
    parser = argparse.ArgumentParser(
        description='Run SDK Tests by providing a corresponding command')

    test_group = parser.add_mutually_exclusive_group(required=True)
    test_group.add_argument(
        '-c', '--command', action='store', type=str, default=None,
        help='Command to be executed')
    test_group.add_argument(
        '-s', '--start', action='store_true', default=False,
        help='Start a series of test runs with the same id')
    test_group.add_argument(
        '-e', '--end', action='store_true', default=False,
        help='End a series of test runs with the same id')

    parser.add_argument(
        '-n', '--name', action='store', type=str,
        help='Name of the software tool (abbreviation)')
    parser.add_argument(
        '--stdout', action='store_true', default=False,
        help='Add STDOUT of the test run to the result')

    parser.add_argument(
        '-l', '--log', action='store', type=str.upper, default="NOTSET",
        help='Log level: NOTSET(default), DEBUG, INFO, WARNING, ERROR, CRITICAL')

    return parser.parse_args()


class TransportAdapter(requests.adapters.HTTPAdapter):

    """
    Transport adapter that allows to use custom ssl_context.
    """

    def __init__(self, ssl_context=None, **kwargs):
        self.ssl_context = ssl_context
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        # save these values for pickling
        self._pool_connections = connections
        self._pool_maxsize = maxsize
        self._pool_block = block

        self.poolmanager = urllib3.poolmanager.PoolManager(
            num_pools=connections, maxsize=maxsize, block=block,
            ssl_context=self.ssl_context, **kwargs)


if __name__ == '__main__':
    args = get_args()
    logger = init_logging(args.log)
    CITestsHandler().run(start=args.start,
                         end=args.end,
                         command=args.command,
                         loglevel=args.log,
                         **{'name': args.name,
                            'stdout': args.stdout})

