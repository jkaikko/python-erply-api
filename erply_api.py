# -*- coding: utf-8 -*-
"""
    ErplyAPI
    ~~~~~~~~

    Simple Python wrapper for Erply API

    :copyright: (c) 2014-2016 by Priit Laes
    :license: BSD, see LICENSE for details.
"""
from contextlib import closing
from datetime import datetime
from time import sleep
import csv
import requests
from requests.exceptions import RequestException

import logging

try:
    # Python 2.7
    from logging import NullHandler
except ImportError:
    class NullHandler:
        def emit(self, record):
            pass

logger = logging.getLogger(__name__)
logger.addHandler(NullHandler())


class ErplyException(Exception):
    """Parent exception."""

    pass

class ErplyAPILimitException(ErplyException):
    """Raised when Erply API limit (by default 1000 requests per hour) has
    been exceeded.

    :param server_time: Erply server time. Can be used to determine amount of
    time until API accepts requests again.
    """

    def __init__(self, server_time):
        self.server_time = server_time


class ErplyPermissionException(ErplyException):
    """No viewing rights for this item."""


class ErplyAuth(object):
    """Object for storing auth information."""

    def __init__(self, code, username, password):
        """Set code, username and password."""
        self.code = code
        self.username = username
        self.password = password

    @property
    def data(self):
        """Convert auth info to dict."""
        return {'username': self.username,
                'password': self.password}


class Erply(object):
    """Erply api wrapper object."""

    ERPLY_GET = (
        # TODO: This list is still incomplete
        'getAddresses',
        'getAddressTypes',
        'getBrands',
        'getCustomers',
        'getCustomerGroups',
        # 'getDocuments'       Unimplemented from ERPLY side :(
        'getEmployees',
        'getPayments',
        'getProducts',
        'getProductCategories',
        'getProductCostForSpecificAmount',  # untested
        'getProductGroups',
        'getProductPrices',  # untested, broken ??
        'getProductPriorityGroups',  # untested
        'getProductStock',
        'getProductUnits',
        'getPurchaseDocuments',
        'getReports',
        'getSalesDocuments',
        'getServices',
        'getWarehouses',
        'verifyUser',
    )
    ERPLY_CSV = ('getProductStockCSV', 'getSalesReport')
    ERPLY_POST = (
        'saveAddress',
        'saveBrand',
        'saveCustomer',
        'saveCustomerGroup',
        'saveInventoryRegistration',
        'saveInventoryTransfer',
        'saveInventoryWriteOff',
        'saveMatrixDimension',
        'saveObject',
        'savePayment',
        'saveProduct',
        'saveProductCategory',
        'saveProductGroup',
        'saveProductPicture',
        'savePurchaseDocument',
        'saveSalesDocument',
        'saveStocktaking',
        'saveStocktakingReadings',
        'saveWarehouse',
    )

    def __init__(self, auth, erply_api_url=None, wait_on_limit=False,
                 session_key=None, error_retry_delay=2):
        self.auth = auth

        # Set _session_key via kwarg. This allows caching the session_key
        # to avoid the initial authentication call.
        self._session_key = session_key

        # Whether to wait for next hour when API limit has been met.
        # When False, ErplyAPILimitException will be raised, otherwise
        # request will be retried when new hour starts.
        self.wait_on_limit = wait_on_limit

        # User-specified Erply API url
        self.erply_api_url = erply_api_url

        # Set the default error retry delay. This is used when encountering a 
        # RequestException
        self.error_retry_delay = error_retry_delay

    @property
    def _payload(self):
        return {'clientCode': self.auth.code}

    @property
    def session(self):
        """Get current session id or authenticate to start a new one."""
        return self._session_key if self._session_key else self.authenticate()

    def authenticate(self):
        """Authenticate with erply api to get a session id."""
        response = self.verifyUser(**self.auth.data)
        if response.error:
            error = "Authentication failed with code {}".format(response.error)
            logger.exception(error)
            raise ErplyException(error)

        session_key = response.fetchone().get('sessionKey', None)
        self._session_key = session_key

        return session_key

    @property
    def payload(self):
        """Get the payload."""
        return dict(sessionKey=self.session, **self._payload)

    @property
    def api_url(self):
        """The url to connect to."""
        return self.erply_api_url or \
            'https://{}.erply.com/api/'.format(self.auth.code)

    def _erply_query(self, data, _initial_response=None):
        """Send request to Erply API and parse response.

        Returns two-tuple containing: `retry` and `data` values:
            - `retry` is boolean specifying whether session token was expired
              and signalling caller to request new session token and redo the
              API with original parameters.
            - `data` - dictionary of original json-encoded response.
        """
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}

        logger.debug('Erply request %s', data.get('request'))

        try:
            resp = requests.post(self.api_url, data=data, headers=headers)
        except RequestException:
            # This might be, for example, a ConnectionError.
            # Sleep a while and then retry once. If it raises the same error,
            # it probably isn't an error that goes away by retrying.
            if self.error_retry_delay:
                sleep(self.error_retry_delay)
            resp = requests.post(self.api_url, data=data, headers=headers)

        if resp.status_code != requests.codes.ok:
            raise ErplyException('Request failed with error {}'.format(
                resp.status_code))

        data = resp.json()
        status = data.get('status', {})

        if not status:
            raise ErplyException('Malformed response')

        error = status.get('errorCode')

        if error == 0:
            return False, data

        elif error == 1002:
            server_time = datetime.fromtimestamp(status.get('requestUnixTime'))

            if not self.wait_on_limit:
                raise ErplyAPILimitException(server_time)

            sleep_time = (60 * (60 - server_time.minute)) + 1
            logger.info('Hourly API limit exceeded, sleeping for %d seconds' %
                        sleep_time)

            # Calculate time to sleep until next hour
            sleep(sleep_time)
            return True, None

        elif error == 1054:
            self._session_key = None
            logger.info('Retrying API call...')
            return True, None

        elif error == 1060:
            # No viewing rights for this item
            logger.info('Permission denied for this resource')
            raise ErplyPermissionException()

        field = status.get('errorField')
        if field:
            raise ErplyException('Erply error: {}, field: {}'.format(
                error, field))

        raise ErplyException('Erply error: {}'.format(error))

    def handle_csv(self, request, *args, **kwargs):
        """Handle a request that expects CSV in return."""
        data = dict(request=request.replace('CSV', ''), responseType='CSV')
        data.update(self.payload)
        data.update(**kwargs)

        retry, parsed_data = self._erply_query(data)
        if retry:
            return getattr(self, request)(*args, **kwargs)

        return ErplyCSVResponse(self, parsed_data)

    def handle_get(self, request, _page=None, _response=None, *args, **kwargs):
        """Request that reads data."""
        _is_bulk = kwargs.pop('_is_bulk', False)
        data = kwargs.copy()
        if _page:
            data['pageNo'] = _page + 1
        if _is_bulk:
            data.update(requestName=request)
            return data

        data.update(request=request)
        data.update(self.payload if request != 'verifyUser' else self._payload)

        retry, parsed_data = self._erply_query(data)

        # Retry request in case of token expiration
        if retry:
            return getattr(self, request)(
                _page=_page, _response=_response, *args, **kwargs)

        if _response:
            _response.populate_page(parsed_data.get('records'), _page)

        return ErplyResponse(self, parsed_data, request, _page, *args, **kwargs)

    def handle_post(self, request, *args, **kwargs):
        """Request that saves data."""
        _is_bulk = kwargs.pop('_is_bulk', False)
        data = kwargs.copy()
        if _is_bulk:
            data.update(requestName=request)
            return data
        data.update(request=request)
        data.update(self.payload)

        retry, parsed_data = self._erply_query(data)

        # Retry request in case of token expiration
        if retry:
            return getattr(self, request)(request, *args, **kwargs)

        return ErplyResponse(self, parsed_data, request, *args, **kwargs)

    def handle_bulk(self, _requests):
        """Bulk request."""
        data = self.payload
        data.update(requests=_requests)
        return ErplyBulkResponse(self, requests.post(self.api_url, data=data))

    def __getattr__(self, attr):
        """Get the correct action.

        Check if the action is listed in ERPLY_GET, ERPLY_POST or ERPLY_CSV,
        else return an exception.
        """
        _attr = None
        _is_bulk = len(attr) > 5 and attr.endswith('_bulk')

        if _is_bulk:
            attr = attr[:-5]

        if attr in self.ERPLY_GET:
            def method(*args, **kwargs):
                _page = kwargs.pop('_page', 0)
                _response = kwargs.pop('_response', None)
                return self.handle_get(attr, _page, _response, _is_bulk=_is_bulk, *args, **kwargs)
            _attr = method

        elif attr in self.ERPLY_POST:
            def method(*args, **kwargs):
                return self.handle_post(attr, _is_bulk=_is_bulk, *args, **kwargs)
            _attr = method

        elif attr in self.ERPLY_CSV:
            def method(*args, **kwargs):
                return self.handle_csv(attr, *args, **kwargs)
            _attr = method

        if _attr:
            self.__dict__[attr] = _attr
            return _attr

        raise ErplyException('Invalid action: %s' % attr)


class ErplyBulkRequest(object):
    """Class that handles bulk requests."""

    def __init__(self, erply, _json_dumps):
        self.calls = []
        self.erply = erply
        self.json_dumper = _json_dumps

    def attach(self, attr, *args, **kwargs):
        if attr in self.erply.ERPLY_GET or attr in self.erply.ERPLY_POST:
            self.calls.append((getattr(self.erply, '{}_bulk'.format(attr)), args, kwargs))

    def __call__(self,):
        _requests = []
        for n, request in enumerate(self.calls, start=1):
            _call, _args, _kwargs = request
            _kwargs.update(requestID=n)
            _requests.append(_call(*_args, **_kwargs))
        return self.erply.handle_bulk(self.json_dumper(_requests))


class ErplyResponse(object):
    """Erply response class."""

    def __init__(self, erply, data, request, page=0, *args, **kwargs):
        self.request = request
        self.erply = erply
        self.error = None

        # Result pagination setup
        self.page = page
        self.per_page = kwargs.get('recordsOnPage', 20)

        self.kwargs = kwargs

        status = data.get('status', {})

        self.total = status.get('recordsTotal')
        self.records = {page: data.get('records')}

    def fetchone(self):
        """Get the first record, if only one exists."""
        if self.total == 1:
            return self.records[0][0]
        elif self.total == 0:
            raise ErplyException('Request returned no results')
        else:
            raise ErplyException(
                'Request returned more than 1 result ({})'.format(self.total))

    def fetch_records(self, page):
        """Get a specific page of results from erply."""
        self.erply.handle_get(self.request, _page=page, _response=self, **self.kwargs)

    def populate_page(self, data, page):
        assert self.per_page != 0
        self.records[page] = data

    def __getitem__(self, key):
        if isinstance(key, slice):
            raise NotImplementedError

        if self.per_page * key >= self.total:
            raise IndexError

        if key not in self.records:
            self.fetch_records(key)

        return self.records[key]


class ErplyCSVResponse(object):
    """CSV Response class."""

    def __init__(self, erply, data):
        self.erply = erply

        status = data.get('status', {})

        self.url = data.get('records').pop().get('reportLink')
        self.timestamp = datetime.fromtimestamp(status.get('requestUnixTime'))

    @property
    def records(self):
        """Make the request and build a csv object from the results."""
        # TODO: Rework so we can use proper iterator...
        with closing(requests.get(self.url, stream=True)) as f:
            if f.status_code != requests.codes.ok:
                raise ValueError
            # XXX: Check whether we have to make it configurable...
            # XXX: Should we remove header and footer?
            return csv.reader(f.text.splitlines(), delimiter=';')


class ErplyBulkResponse(object):
    """Bulk response class."""

    # TODO: This class will be reworked in the future..
    def __init__(self, erply, response):
        if response.status_code != requests.codes.ok:
            print ('Request failed with error code {}'.format(response.status_code))
            raise ValueError

        self.data = response.json()
        status = self.data.get('status', {})
        if not status:
            print ("Malformed response")
            raise ValueError

        self.error = status.get('errorCode')
        self._requests = self.data.get('requests')

    @property
    def records(self):
        """Get the results."""
        if self._requests is None:
            raise ValueError
        for el in self._requests:
            _status = el.get('status')
            if _status.get('responseStatus') == 'error':
                print ('Request failed: requestID: {} errorField: {}'.format(
                       _status.get('requestID'),
                       _status.get('errorField'),
                       ))
            else:
                yield el.get('records')
