# coding: utf-8
"""
Pagination serializers determine the structure of the output that should
be used for paginated responses.
"""
from __future__ import unicode_literals
from base64 import b64encode, b64decode
from collections import namedtuple
from django.core.paginator import InvalidPage, Paginator as DjangoPaginator
from django.template import Context, loader
from django.utils import six
from django.utils.six.moves.urllib import parse as urlparse
from django.utils.translation import ugettext_lazy as _
from rest_framework.compat import OrderedDict
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.utils.urls import (
    replace_query_param, remove_query_param
)
import warnings


def _positive_int(integer_string, strict=False, cutoff=None):
    """
    Cast a string to a strictly positive integer.
    """
    ret = int(integer_string)
    if ret < 0 or (ret == 0 and strict):
        raise ValueError()
    if cutoff:
        ret = min(ret, cutoff)
    return ret


def _divide_with_ceil(a, b):
    """
    Returns 'a' divded by 'b', with any remainder rounded up.
    """
    if a % b:
        return (a // b) + 1
    return a // b


def _get_count(queryset):
    """
    Determine an object count, supporting either querysets or regular lists.
    """
    try:
        return queryset.count()
    except (AttributeError, TypeError):
        return len(queryset)


def _get_displayed_page_numbers(current, final):
    """
    This utility function determines a list of page numbers to display.
    This gives us a nice contextually relevant set of page numbers.

    For example:
    current=14, final=16 -> [1, None, 13, 14, 15, 16]

    This implementation gives one page to each side of the cursor,
    or two pages to the side when the cursor is at the edge, then
    ensures that any breaks between non-continous page numbers never
    remove only a single page.

    For an alernativative implementation which gives two pages to each side of
    the cursor, eg. as in GitHub issue list pagination, see:

    https://gist.github.com/tomchristie/321140cebb1c4a558b15
    """
    assert current >= 1
    assert final >= current

    if final <= 5:
        return list(range(1, final + 1))

    # We always include the first two pages, last two pages, and
    # two pages either side of the current page.
    included = set((
        1,
        current - 1, current, current + 1,
        final
    ))

    # If the break would only exclude a single page number then we
    # may as well include the page number instead of the break.
    if current <= 4:
        included.add(2)
        included.add(3)
    if current >= final - 3:
        included.add(final - 1)
        included.add(final - 2)

    # Now sort the page numbers and drop anything outside the limits.
    included = [
        idx for idx in sorted(list(included))
        if idx > 0 and idx <= final
    ]

    # Finally insert any `...` breaks
    if current > 4:
        included.insert(1, None)
    if current < final - 3:
        included.insert(len(included) - 1, None)
    return included


def _get_page_links(page_numbers, current, url_func):
    """
    Given a list of page numbers and `None` page breaks,
    return a list of `PageLink` objects.
    """
    page_links = []
    for page_number in page_numbers:
        if page_number is None:
            page_link = PAGE_BREAK
        else:
            page_link = PageLink(
                url=url_func(page_number),
                number=page_number,
                is_active=(page_number == current),
                is_break=False
            )
        page_links.append(page_link)
    return page_links


def _decode_cursor(encoded):
    """
    Given a string representing an encoded cursor, return a `Cursor` instance.
    """

    # The offset in the cursor is used in situations where we have a
    # nearly-unique index. (Eg millisecond precision creation timestamps)
    # We guard against malicious users attempting to cause expensive database
    # queries, by having a hard cap on the maximum possible size of the offset.
    OFFSET_CUTOFF = 1000

    try:
        querystring = b64decode(encoded.encode('ascii')).decode('ascii')
        tokens = urlparse.parse_qs(querystring, keep_blank_values=True)

        offset = tokens.get('o', ['0'])[0]
        offset = _positive_int(offset, cutoff=OFFSET_CUTOFF)

        reverse = tokens.get('r', ['0'])[0]
        reverse = bool(int(reverse))

        position = tokens.get('p', [None])[0]
    except (TypeError, ValueError):
        return None

    return Cursor(offset=offset, reverse=reverse, position=position)


def _encode_cursor(cursor):
    """
    Given a Cursor instance, return an encoded string representation.
    """
    tokens = {}
    if cursor.offset != 0:
        tokens['o'] = str(cursor.offset)
    if cursor.reverse:
        tokens['r'] = '1'
    if cursor.position is not None:
        tokens['p'] = cursor.position

    querystring = urlparse.urlencode(tokens, doseq=True)
    return b64encode(querystring.encode('ascii')).decode('ascii')


def _reverse_ordering(ordering_tuple):
    """
    Given an order_by tuple such as `('-created', 'uuid')` reverse the
    ordering and return a new tuple, eg. `('created', '-uuid')`.
    """
    def invert(x):
        return x[1:] if (x.startswith('-')) else '-' + x

    return tuple([invert(item) for item in ordering_tuple])


Cursor = namedtuple('Cursor', ['offset', 'reverse', 'position'])
PageLink = namedtuple('PageLink', ['url', 'number', 'is_active', 'is_break'])

PAGE_BREAK = PageLink(url=None, number=None, is_active=False, is_break=True)


class BasePagination(object):
    display_page_controls = False

    def paginate_queryset(self, queryset, request, view=None):  # pragma: no cover
        raise NotImplementedError('paginate_queryset() must be implemented.')

    def get_paginated_response(self, data):  # pragma: no cover
        raise NotImplementedError('get_paginated_response() must be implemented.')

    def to_html(self):  # pragma: no cover
        raise NotImplementedError('to_html() must be implemented to display page controls.')

    def get_results(self, data):
        return data['results']


class PageNumberPagination(BasePagination):
    """
    A simple page number based style that supports page numbers as
    query parameters. For example:

    http://api.example.org/accounts/?page=4
    http://api.example.org/accounts/?page=4&page_size=100
    """
    # The default page size.
    # Defaults to `None`, meaning pagination is disabled.
    page_size = api_settings.PAGE_SIZE

    # Client can control the page using this query parameter.
    page_query_param = 'page'

    # Client can control the page size using this query parameter.
    # Default is 'None'. Set to eg 'page_size' to enable usage.
    page_size_query_param = None

    # Set to an integer to limit the maximum page size the client may request.
    # Only relevant if 'page_size_query_param' has also been set.
    max_page_size = None

    last_page_strings = ('last',)

    template = 'rest_framework/pagination/numbers.html'

    invalid_page_message = _('Invalid page "{page_number}": {message}.')

    def _handle_backwards_compat(self, view):
        """
        Prior to version 3.1, pagination was handled in the view, and the
        attributes were set there. The attributes should now be set on
        the pagination class, but the old style is still pending deprecation.
        """
        assert not (
            getattr(view, 'pagination_serializer_class', None) or
            getattr(api_settings, 'DEFAULT_PAGINATION_SERIALIZER_CLASS', None)
        ), (
            "The pagination_serializer_class attribute and "
            "DEFAULT_PAGINATION_SERIALIZER_CLASS setting have been removed as "
            "part of the 3.1 pagination API improvement. See the pagination "
            "documentation for details on the new API."
        )

        for (settings_key, attr_name) in (
            ('PAGINATE_BY', 'page_size'),
            ('PAGINATE_BY_PARAM', 'page_size_query_param'),
            ('MAX_PAGINATE_BY', 'max_page_size')
        ):
            value = getattr(api_settings, settings_key, None)
            if value is not None:
                setattr(self, attr_name, value)
                warnings.warn(
                    "The `%s` settings key is pending deprecation. "
                    "Use the `%s` attribute on the pagination class instead." % (
                        settings_key, attr_name
                    ),
                    PendingDeprecationWarning,
                )

        for (view_attr, attr_name) in (
            ('paginate_by', 'page_size'),
            ('page_query_param', 'page_query_param'),
            ('paginate_by_param', 'page_size_query_param'),
            ('max_paginate_by', 'max_page_size')
        ):
            value = getattr(view, view_attr, None)
            if value is not None:
                setattr(self, attr_name, value)
                warnings.warn(
                    "The `%s` view attribute is pending deprecation. "
                    "Use the `%s` attribute on the pagination class instead." % (
                        view_attr, attr_name
                    ),
                    PendingDeprecationWarning,
                )

    def paginate_queryset(self, queryset, request, view=None):
        """
        Paginate a queryset if required, either returning a
        page object, or `None` if pagination is not configured for this view.
        """
        self._handle_backwards_compat(view)

        page_size = self.get_page_size(request)
        if not page_size:
            return None

        paginator = DjangoPaginator(queryset, page_size)
        page_number = request.query_params.get(self.page_query_param, 1)
        if page_number in self.last_page_strings:
            page_number = paginator.num_pages

        try:
            self.page = paginator.page(page_number)
        except InvalidPage as exc:
            msg = self.invalid_page_message.format(
                page_number=page_number, message=six.text_type(exc)
            )
            raise NotFound(msg)

        if paginator.num_pages > 1 and self.template is not None:
            # The browsable API should display pagination controls.
            self.display_page_controls = True

        self.request = request
        return list(self.page)

    def get_paginated_response(self, data):
        return Response(OrderedDict([
            ('count', self.page.paginator.count),
            ('next', self.get_next_link()),
            ('previous', self.get_previous_link()),
            ('results', data)
        ]))

    def get_page_size(self, request):
        if self.page_size_query_param:
            try:
                return _positive_int(
                    request.query_params[self.page_size_query_param],
                    strict=True,
                    cutoff=self.max_page_size
                )
            except (KeyError, ValueError):
                pass

        return self.page_size

    def get_next_link(self):
        if not self.page.has_next():
            return None
        url = self.request.build_absolute_uri()
        page_number = self.page.next_page_number()
        return replace_query_param(url, self.page_query_param, page_number)

    def get_previous_link(self):
        if not self.page.has_previous():
            return None
        url = self.request.build_absolute_uri()
        page_number = self.page.previous_page_number()
        if page_number == 1:
            return remove_query_param(url, self.page_query_param)
        return replace_query_param(url, self.page_query_param, page_number)

    def get_html_context(self):
        base_url = self.request.build_absolute_uri()

        def page_number_to_url(page_number):
            if page_number == 1:
                return remove_query_param(base_url, self.page_query_param)
            else:
                return replace_query_param(base_url, self.page_query_param, page_number)

        current = self.page.number
        final = self.page.paginator.num_pages
        page_numbers = _get_displayed_page_numbers(current, final)
        page_links = _get_page_links(page_numbers, current, page_number_to_url)

        return {
            'previous_url': self.get_previous_link(),
            'next_url': self.get_next_link(),
            'page_links': page_links
        }

    def to_html(self):
        template = loader.get_template(self.template)
        context = Context(self.get_html_context())
        return template.render(context)


class LimitOffsetPagination(BasePagination):
    """
    A limit/offset based style. For example:

    http://api.example.org/accounts/?limit=100
    http://api.example.org/accounts/?offset=400&limit=100
    """
    default_limit = api_settings.PAGE_SIZE
    limit_query_param = 'limit'
    offset_query_param = 'offset'
    max_limit = None
    template = 'rest_framework/pagination/numbers.html'

    def paginate_queryset(self, queryset, request, view=None):
        self.limit = self.get_limit(request)
        if self.limit is None:
            return None

        self.offset = self.get_offset(request)
        self.count = _get_count(queryset)
        self.request = request
        if self.count > self.limit and self.template is not None:
            self.display_page_controls = True
        return list(queryset[self.offset:self.offset + self.limit])

    def get_paginated_response(self, data):
        return Response(OrderedDict([
            ('count', self.count),
            ('next', self.get_next_link()),
            ('previous', self.get_previous_link()),
            ('results', data)
        ]))

    def get_limit(self, request):
        if self.limit_query_param:
            try:
                return _positive_int(
                    request.query_params[self.limit_query_param],
                    cutoff=self.max_limit
                )
            except (KeyError, ValueError):
                pass

        return self.default_limit

    def get_offset(self, request):
        try:
            return _positive_int(
                request.query_params[self.offset_query_param],
            )
        except (KeyError, ValueError):
            return 0

    def get_next_link(self):
        if self.offset + self.limit >= self.count:
            return None

        url = self.request.build_absolute_uri()
        offset = self.offset + self.limit
        return replace_query_param(url, self.offset_query_param, offset)

    def get_previous_link(self):
        if self.offset <= 0:
            return None

        url = self.request.build_absolute_uri()

        if self.offset - self.limit <= 0:
            return remove_query_param(url, self.offset_query_param)

        offset = self.offset - self.limit
        return replace_query_param(url, self.offset_query_param, offset)

    def get_html_context(self):
        base_url = self.request.build_absolute_uri()
        current = _divide_with_ceil(self.offset, self.limit) + 1
        # The number of pages is a little bit fiddly.
        # We need to sum both the number of pages from current offset to end
        # plus the number of pages up to the current offset.
        # When offset is not strictly divisible by the limit then we may
        # end up introducing an extra page as an artifact.
        final = (
            _divide_with_ceil(self.count - self.offset, self.limit) +
            _divide_with_ceil(self.offset, self.limit)
        )

        def page_number_to_url(page_number):
            if page_number == 1:
                return remove_query_param(base_url, self.offset_query_param)
            else:
                offset = self.offset + ((page_number - current) * self.limit)
                return replace_query_param(base_url, self.offset_query_param, offset)

        page_numbers = _get_displayed_page_numbers(current, final)
        page_links = _get_page_links(page_numbers, current, page_number_to_url)

        return {
            'previous_url': self.get_previous_link(),
            'next_url': self.get_next_link(),
            'page_links': page_links
        }

    def to_html(self):
        template = loader.get_template(self.template)
        context = Context(self.get_html_context())
        return template.render(context)


class CursorPagination(BasePagination):
    """
    The cursor pagination implementation is neccessarily complex.
    For an overview of the position/offset style we use, see this post:
    http://cramer.io/2011/03/08/building-cursors-for-the-disqus-api/
    """
    cursor_query_param = 'cursor'
    page_size = api_settings.PAGE_SIZE
    invalid_cursor_message = _('Invalid cursor')
    ordering = '-created'
    template = 'rest_framework/pagination/previous_and_next.html'

    def paginate_queryset(self, queryset, request, view=None):
        if self.page_size is None:
            return None

        self.base_url = request.build_absolute_uri()
        self.ordering = self.get_ordering(request, queryset, view)

        # Determine if we have a cursor, and if so then decode it.
        encoded = request.query_params.get(self.cursor_query_param)
        if encoded is None:
            self.cursor = None
            (offset, reverse, current_position) = (0, False, None)
        else:
            self.cursor = _decode_cursor(encoded)
            if self.cursor is None:
                raise NotFound(self.invalid_cursor_message)
            (offset, reverse, current_position) = self.cursor

        # Cursor pagination always enforces an ordering.
        if reverse:
            queryset = queryset.order_by(*_reverse_ordering(self.ordering))
        else:
            queryset = queryset.order_by(*self.ordering)

        # If we have a cursor with a fixed position then filter by that.
        if current_position is not None:
            order = self.ordering[0]
            is_reversed = order.startswith('-')
            order_attr = order.lstrip('-')

            # Test for: (cursor reversed) XOR (queryset reversed)
            if self.cursor.reverse != is_reversed:
                kwargs = {order_attr + '__lt': current_position}
            else:
                kwargs = {order_attr + '__gt': current_position}

            queryset = queryset.filter(**kwargs)

        # If we have an offset cursor then offset the entire page by that amount.
        # We also always fetch an extra item in order to determine if there is a
        # page following on from this one.
        results = list(queryset[offset:offset + self.page_size + 1])
        self.page = list(results[:self.page_size])

        # Determine the position of the final item following the page.
        if len(results) > len(self.page):
            has_following_postion = True
            following_position = self._get_position_from_instance(results[-1], self.ordering)
        else:
            has_following_postion = False
            following_position = None

        # If we have a reverse queryset, then the query ordering was in reverse
        # so we need to reverse the items again before returning them to the user.
        if reverse:
            self.page = list(reversed(self.page))

        if reverse:
            # Determine next and previous positions for reverse cursors.
            self.has_next = (current_position is not None) or (offset > 0)
            self.has_previous = has_following_postion
            if self.has_next:
                self.next_position = current_position
            if self.has_previous:
                self.previous_position = following_position
        else:
            # Determine next and previous positions for forward cursors.
            self.has_next = has_following_postion
            self.has_previous = (current_position is not None) or (offset > 0)
            if self.has_next:
                self.next_position = following_position
            if self.has_previous:
                self.previous_position = current_position

        # Display page controls in the browsable API if there is more
        # than one page.
        if (self.has_previous or self.has_next) and self.template is not None:
            self.display_page_controls = True

        return self.page

    def get_next_link(self):
        if not self.has_next:
            return None

        if self.cursor and self.cursor.reverse and self.cursor.offset != 0:
            # If we're reversing direction and we have an offset cursor
            # then we cannot use the first position we find as a marker.
            compare = self._get_position_from_instance(self.page[-1], self.ordering)
        else:
            compare = self.next_position
        offset = 0

        for item in reversed(self.page):
            position = self._get_position_from_instance(item, self.ordering)
            if position != compare:
                # The item in this position and the item following it
                # have different positions. We can use this position as
                # our marker.
                break

            # The item in this postion has the same position as the item
            # following it, we can't use it as a marker position, so increment
            # the offset and keep seeking to the previous item.
            compare = position
            offset += 1

        else:
            # There were no unique positions in the page.
            if not self.has_previous:
                # We are on the first page.
                # Our cursor will have an offset equal to the page size,
                # but no position to filter against yet.
                offset = self.page_size
                position = None
            elif self.cursor.reverse:
                # The change in direction will introduce a paging artifact,
                # where we end up skipping forward a few extra items.
                offset = 0
                position = self.previous_position
            else:
                # Use the position from the existing cursor and increment
                # it's offset by the page size.
                offset = self.cursor.offset + self.page_size
                position = self.previous_position

        cursor = Cursor(offset=offset, reverse=False, position=position)
        encoded = _encode_cursor(cursor)
        return replace_query_param(self.base_url, self.cursor_query_param, encoded)

    def get_previous_link(self):
        if not self.has_previous:
            return None

        if self.cursor and not self.cursor.reverse and self.cursor.offset != 0:
            # If we're reversing direction and we have an offset cursor
            # then we cannot use the first position we find as a marker.
            compare = self._get_position_from_instance(self.page[0], self.ordering)
        else:
            compare = self.previous_position
        offset = 0

        for item in self.page:
            position = self._get_position_from_instance(item, self.ordering)
            if position != compare:
                # The item in this position and the item following it
                # have different positions. We can use this position as
                # our marker.
                break

            # The item in this postion has the same position as the item
            # following it, we can't use it as a marker position, so increment
            # the offset and keep seeking to the previous item.
            compare = position
            offset += 1

        else:
            # There were no unique positions in the page.
            if not self.has_next:
                # We are on the final page.
                # Our cursor will have an offset equal to the page size,
                # but no position to filter against yet.
                offset = self.page_size
                position = None
            elif self.cursor.reverse:
                # Use the position from the existing cursor and increment
                # it's offset by the page size.
                offset = self.cursor.offset + self.page_size
                position = self.next_position
            else:
                # The change in direction will introduce a paging artifact,
                # where we end up skipping back a few extra items.
                offset = 0
                position = self.next_position

        cursor = Cursor(offset=offset, reverse=True, position=position)
        encoded = _encode_cursor(cursor)
        return replace_query_param(self.base_url, self.cursor_query_param, encoded)

    def get_ordering(self, request, queryset, view):
        """
        Return a tuple of strings, that may be used in an `order_by` method.
        """
        ordering_filters = [
            filter_cls for filter_cls in getattr(view, 'filter_backends', [])
            if hasattr(filter_cls, 'get_ordering')
        ]

        if ordering_filters:
            # If a filter exists on the view that implements `get_ordering`
            # then we defer to that filter to determine the ordering.
            filter_cls = ordering_filters[0]
            filter_instance = filter_cls()
            ordering = filter_instance.get_ordering(request, queryset, view)
            assert ordering is not None, (
                'Using cursor pagination, but filter class {filter_cls} '
                'returned a `None` ordering.'.format(
                    filter_cls=filter_cls.__name__
                )
            )
        else:
            # The default case is to check for an `ordering` attribute
            # on this pagination instance.
            ordering = self.ordering
            assert ordering is not None, (
                'Using cursor pagination, but no ordering attribute was declared '
                'on the pagination class.'
            )

        assert isinstance(ordering, (six.string_types, list, tuple)), (
            'Invalid ordering. Expected string or tuple, but got {type}'.format(
                type=type(ordering).__name__
            )
        )

        if isinstance(ordering, six.string_types):
            return (ordering,)
        return tuple(ordering)

    def _get_position_from_instance(self, instance, ordering):
        attr = getattr(instance, ordering[0].lstrip('-'))
        return six.text_type(attr)

    def get_paginated_response(self, data):
        return Response(OrderedDict([
            ('next', self.get_next_link()),
            ('previous', self.get_previous_link()),
            ('results', data)
        ]))

    def get_html_context(self):
        return {
            'previous_url': self.get_previous_link(),
            'next_url': self.get_next_link()
        }

    def to_html(self):
        template = loader.get_template(self.template)
        context = Context(self.get_html_context())
        return template.render(context)
