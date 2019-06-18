"""Routines related to PyPI, indexes"""
from __future__ import absolute_import

import cgi
import itertools
import logging
import mimetypes
import os
import re
from collections import namedtuple

from pip._vendor import html5lib, requests, six
from pip._vendor.distlib.compat import unescape
from pip._vendor.packaging import specifiers
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import parse as parse_version
from pip._vendor.requests.exceptions import HTTPError, RetryError, SSLError
from pip._vendor.six.moves.urllib import parse as urllib_parse
from pip._vendor.six.moves.urllib import request as urllib_request

from pip._internal.download import is_url, url_to_path
from pip._internal.exceptions import (
    BestVersionAlreadyInstalled, DistributionNotFound, InvalidWheelFilename,
    UnsupportedWheel,
)
from pip._internal.models.candidate import InstallationCandidate
from pip._internal.models.format_control import FormatControl
from pip._internal.models.link import Link
from pip._internal.models.search_scope import SearchScope
from pip._internal.models.target_python import TargetPython
from pip._internal.utils.compat import ipaddress
from pip._internal.utils.logging import indent_log
from pip._internal.utils.misc import (
    ARCHIVE_EXTENSIONS, SUPPORTED_EXTENSIONS, WHEEL_EXTENSION, path_to_url,
    redact_password_from_url,
)
from pip._internal.utils.packaging import check_requires_python
from pip._internal.utils.typing import MYPY_CHECK_RUNNING
from pip._internal.wheel import Wheel

if MYPY_CHECK_RUNNING:
    from logging import Logger
    from typing import (
        Any, Callable, Iterable, Iterator, List, MutableMapping, Optional,
        Sequence, Set, Tuple, Union,
    )
    from pip._vendor.packaging.version import _BaseVersion
    from pip._vendor.requests import Response
    from pip._internal.req import InstallRequirement
    from pip._internal.download import PipSession

    SecureOrigin = Tuple[str, str, Optional[str]]
    BuildTag = Tuple[Any, ...]  # either empty tuple or Tuple[int, str]
    CandidateSortingKey = Tuple[int, _BaseVersion, BuildTag, Optional[int]]


__all__ = ['FormatControl', 'FoundCandidates', 'PackageFinder']


SECURE_ORIGINS = [
    # protocol, hostname, port
    # Taken from Chrome's list of secure origins (See: http://bit.ly/1qrySKC)
    ("https", "*", "*"),
    ("*", "localhost", "*"),
    ("*", "127.0.0.0/8", "*"),
    ("*", "::1/128", "*"),
    ("file", "*", None),
    # ssh is always secure.
    ("ssh", "*", "*"),
]  # type: List[SecureOrigin]


logger = logging.getLogger(__name__)


def _match_vcs_scheme(url):
    # type: (str) -> Optional[str]
    """Look for VCS schemes in the URL.

    Returns the matched VCS scheme, or None if there's no match.
    """
    from pip._internal.vcs import vcs
    for scheme in vcs.schemes:
        if url.lower().startswith(scheme) and url[len(scheme)] in '+:':
            return scheme
    return None


def _is_url_like_archive(url):
    # type: (str) -> bool
    """Return whether the URL looks like an archive.
    """
    filename = Link(url).filename
    for bad_ext in ARCHIVE_EXTENSIONS:
        if filename.endswith(bad_ext):
            return True
    return False


class _NotHTML(Exception):
    def __init__(self, content_type, request_desc):
        # type: (str, str) -> None
        super(_NotHTML, self).__init__(content_type, request_desc)
        self.content_type = content_type
        self.request_desc = request_desc


def _ensure_html_header(response):
    # type: (Response) -> None
    """Check the Content-Type header to ensure the response contains HTML.

    Raises `_NotHTML` if the content type is not text/html.
    """
    content_type = response.headers.get("Content-Type", "")
    if not content_type.lower().startswith("text/html"):
        raise _NotHTML(content_type, response.request.method)


class _NotHTTP(Exception):
    pass


def _ensure_html_response(url, session):
    # type: (str, PipSession) -> None
    """Send a HEAD request to the URL, and ensure the response contains HTML.

    Raises `_NotHTTP` if the URL is not available for a HEAD request, or
    `_NotHTML` if the content type is not text/html.
    """
    scheme, netloc, path, query, fragment = urllib_parse.urlsplit(url)
    if scheme not in {'http', 'https'}:
        raise _NotHTTP()

    resp = session.head(url, allow_redirects=True)
    resp.raise_for_status()

    _ensure_html_header(resp)


def _get_html_response(url, session):
    # type: (str, PipSession) -> Response
    """Access an HTML page with GET, and return the response.

    This consists of three parts:

    1. If the URL looks suspiciously like an archive, send a HEAD first to
       check the Content-Type is HTML, to avoid downloading a large file.
       Raise `_NotHTTP` if the content type cannot be determined, or
       `_NotHTML` if it is not HTML.
    2. Actually perform the request. Raise HTTP exceptions on network failures.
    3. Check the Content-Type header to make sure we got HTML, and raise
       `_NotHTML` otherwise.
    """
    if _is_url_like_archive(url):
        _ensure_html_response(url, session=session)

    logger.debug('Getting page %s', redact_password_from_url(url))

    resp = session.get(
        url,
        headers={
            "Accept": "text/html",
            # We don't want to blindly returned cached data for
            # /simple/, because authors generally expecting that
            # twine upload && pip install will function, but if
            # they've done a pip install in the last ~10 minutes
            # it won't. Thus by setting this to zero we will not
            # blindly use any cached data, however the benefit of
            # using max-age=0 instead of no-cache, is that we will
            # still support conditional requests, so we will still
            # minimize traffic sent in cases where the page hasn't
            # changed at all, we will just always incur the round
            # trip for the conditional GET now instead of only
            # once per 10 minutes.
            # For more information, please see pypa/pip#5670.
            "Cache-Control": "max-age=0",
        },
    )
    resp.raise_for_status()

    # The check for archives above only works if the url ends with
    # something that looks like an archive. However that is not a
    # requirement of an url. Unless we issue a HEAD request on every
    # url we cannot know ahead of time for sure if something is HTML
    # or not. However we can check after we've downloaded it.
    _ensure_html_header(resp)

    return resp


def _handle_get_page_fail(
    link,  # type: Link
    reason,  # type: Union[str, Exception]
    meth=None  # type: Optional[Callable[..., None]]
):
    # type: (...) -> None
    if meth is None:
        meth = logger.debug
    meth("Could not fetch URL %s: %s - skipping", link, reason)


def _get_html_page(link, session=None):
    # type: (Link, Optional[PipSession]) -> Optional[HTMLPage]
    if session is None:
        raise TypeError(
            "_get_html_page() missing 1 required keyword argument: 'session'"
        )

    url = link.url.split('#', 1)[0]

    # Check for VCS schemes that do not support lookup as web pages.
    vcs_scheme = _match_vcs_scheme(url)
    if vcs_scheme:
        logger.debug('Cannot look at %s URL %s', vcs_scheme, link)
        return None

    # Tack index.html onto file:// URLs that point to directories
    scheme, _, path, _, _, _ = urllib_parse.urlparse(url)
    if (scheme == 'file' and os.path.isdir(urllib_request.url2pathname(path))):
        # add trailing slash if not present so urljoin doesn't trim
        # final segment
        if not url.endswith('/'):
            url += '/'
        url = urllib_parse.urljoin(url, 'index.html')
        logger.debug(' file: URL is directory, getting %s', url)

    try:
        resp = _get_html_response(url, session=session)
    except _NotHTTP:
        logger.debug(
            'Skipping page %s because it looks like an archive, and cannot '
            'be checked by HEAD.', link,
        )
    except _NotHTML as exc:
        logger.debug(
            'Skipping page %s because the %s request got Content-Type: %s',
            link, exc.request_desc, exc.content_type,
        )
    except HTTPError as exc:
        _handle_get_page_fail(link, exc)
    except RetryError as exc:
        _handle_get_page_fail(link, exc)
    except SSLError as exc:
        reason = "There was a problem confirming the ssl certificate: "
        reason += str(exc)
        _handle_get_page_fail(link, reason, meth=logger.info)
    except requests.ConnectionError as exc:
        _handle_get_page_fail(link, "connection error: %s" % exc)
    except requests.Timeout:
        _handle_get_page_fail(link, "timed out")
    else:
        return HTMLPage(resp.content, resp.url, resp.headers)
    return None


def _check_link_requires_python(
    link,  # type: Link
    version_info,  # type: Tuple[int, int, int]
    ignore_requires_python=False,  # type: bool
):
    # type: (...) -> bool
    """
    Return whether the given Python version is compatible with a link's
    "Requires-Python" value.

    :param version_info: A 3-tuple of ints representing the Python
        major-minor-micro version to check.
    :param ignore_requires_python: Whether to ignore the "Requires-Python"
        value if the given Python version isn't compatible.
    """
    try:
        is_compatible = check_requires_python(
            link.requires_python, version_info=version_info,
        )
    except specifiers.InvalidSpecifier:
        logger.debug(
            "Ignoring invalid Requires-Python (%r) for link: %s",
            link.requires_python, link,
        )
    else:
        if not is_compatible:
            version = '.'.join(map(str, version_info))
            if not ignore_requires_python:
                logger.debug(
                    'Link requires a different Python (%s not in: %r): %s',
                    version, link.requires_python, link,
                )
                return False

            logger.debug(
                'Ignoring failed Requires-Python check (%s not in: %r) '
                'for link: %s',
                version, link.requires_python, link,
            )

    return True


class CandidateEvaluator(object):

    """
    Responsible for filtering and sorting candidates for installation based
    on what tags are valid.
    """

    def __init__(
        self,
        target_python=None,  # type: Optional[TargetPython]
        prefer_binary=False,   # type: bool
        allow_all_prereleases=False,  # type: bool
        ignore_requires_python=None,  # type: Optional[bool]
    ):
        # type: (...) -> None
        """
        :param target_python: The target Python interpreter to use to check
            both the Python version embedded in the filename and the package's
            "Requires-Python" metadata. If None (the default), then a
            TargetPython object will be constructed from the running Python.
        :param allow_all_prereleases: Whether to allow all pre-releases.
        :param ignore_requires_python: Whether to ignore incompatible
            "Requires-Python" values in links. Defaults to False.
        """
        if target_python is None:
            target_python = TargetPython()
        if ignore_requires_python is None:
            ignore_requires_python = False

        self._ignore_requires_python = ignore_requires_python
        self._prefer_binary = prefer_binary
        self._target_python = target_python

        # We compile the regex here instead of as a class attribute so as
        # not to impact pip start-up time.  This is also okay because
        # CandidateEvaluator is generally instantiated only once per pip
        # invocation (when PackageFinder is instantiated).
        self._py_version_re = re.compile(r'-py([123]\.?[0-9]?)$')

        self.allow_all_prereleases = allow_all_prereleases

    def _is_wheel_supported(self, wheel):
        # type: (Wheel) -> bool
        valid_tags = self._target_python.get_tags()
        return wheel.supported(valid_tags)

    def evaluate_link(self, link, search):
        # type: (Link, Search) -> Tuple[bool, Optional[str]]
        """
        Determine whether a link is a candidate for installation.

        :return: A tuple (is_candidate, result), where `result` is (1) a
            version string if `is_candidate` is True, and (2) if
            `is_candidate` is False, an optional string to log the reason
            the link fails to qualify.
        """
        version = None
        if link.egg_fragment:
            egg_info = link.egg_fragment
            ext = link.ext
        else:
            egg_info, ext = link.splitext()
            if not ext:
                return (False, 'not a file')
            if ext not in SUPPORTED_EXTENSIONS:
                return (False, 'unsupported archive format: %s' % ext)
            if "binary" not in search.formats and ext == WHEEL_EXTENSION:
                reason = 'No binaries permitted for %s' % search.supplied
                return (False, reason)
            if "macosx10" in link.path and ext == '.zip':
                return (False, 'macosx10 one')
            if ext == WHEEL_EXTENSION:
                try:
                    wheel = Wheel(link.filename)
                except InvalidWheelFilename:
                    return (False, 'invalid wheel filename')
                if canonicalize_name(wheel.name) != search.canonical:
                    reason = 'wrong project name (not %s)' % search.supplied
                    return (False, reason)

                if not self._is_wheel_supported(wheel):
                    # Include the wheel's tags in the reason string to
                    # simplify troubleshooting compatibility issues.
                    file_tags = wheel.get_formatted_file_tags()
                    reason = (
                        "none of the wheel's tags match: {}".format(
                            ', '.join(file_tags)
                        )
                    )
                    return (False, reason)

                version = wheel.version

        # This should be up by the search.ok_binary check, but see issue 2700.
        if "source" not in search.formats and ext != WHEEL_EXTENSION:
            return (False, 'No sources permitted for %s' % search.supplied)

        if not version:
            version = _egg_info_matches(egg_info, search.canonical)
        if not version:
            return (False, 'Missing project version for %s' % search.supplied)

        match = self._py_version_re.search(version)
        if match:
            version = version[:match.start()]
            py_version = match.group(1)
            if py_version != self._target_python.py_version:
                return (False, 'Python version is incorrect')

        supports_python = _check_link_requires_python(
            link, version_info=self._target_python.py_version_info,
            ignore_requires_python=self._ignore_requires_python,
        )
        if not supports_python:
            # Return None for the reason text to suppress calling
            # _log_skipped_link().
            return (False, None)

        logger.debug('Found link %s, version: %s', link, version)

        return (True, version)

    def make_found_candidates(
        self,
        candidates,      # type: List[InstallationCandidate]
        specifier=None,  # type: Optional[specifiers.BaseSpecifier]
    ):
        # type: (...) -> FoundCandidates
        """
        Create and return a `FoundCandidates` instance.

        :param specifier: An optional object implementing `filter`
            (e.g. `packaging.specifiers.SpecifierSet`) to filter applicable
            versions.
        """
        if specifier is None:
            specifier = specifiers.SpecifierSet()

        # Using None infers from the specifier instead.
        allow_prereleases = self.allow_all_prereleases or None
        versions = {
            str(v) for v in specifier.filter(
                # We turn the version object into a str here because otherwise
                # when we're debundled but setuptools isn't, Python will see
                # packaging.version.Version and
                # pkg_resources._vendor.packaging.version.Version as different
                # types. This way we'll use a str as a common data interchange
                # format. If we stop using the pkg_resources provided specifier
                # and start using our own, we can drop the cast to str().
                (str(c.version) for c in candidates),
                prereleases=allow_prereleases,
            )
        }
        return FoundCandidates(candidates, versions=versions, evaluator=self)

    def _sort_key(self, candidate):
        # type: (InstallationCandidate) -> CandidateSortingKey
        """
        Function used to generate link sort key for link tuples.
        The greater the return value, the more preferred it is.
        If not finding wheels, then sorted by version only.
        If finding wheels, then the sort order is by version, then:
          1. existing installs
          2. wheels ordered via Wheel.support_index_min(self._valid_tags)
          3. source archives
        If prefer_binary was set, then all wheels are sorted above sources.
        Note: it was considered to embed this logic into the Link
              comparison operators, but then different sdist links
              with the same version, would have to be considered equal
        """
        valid_tags = self._target_python.get_tags()
        support_num = len(valid_tags)
        build_tag = tuple()  # type: BuildTag
        binary_preference = 0
        if candidate.location.is_wheel:
            # can raise InvalidWheelFilename
            wheel = Wheel(candidate.location.filename)
            if not self._is_wheel_supported(wheel):
                raise UnsupportedWheel(
                    "%s is not a supported wheel for this platform. It "
                    "can't be sorted." % wheel.filename
                )
            if self._prefer_binary:
                binary_preference = 1
            pri = -(wheel.support_index_min(valid_tags))
            if wheel.build_tag is not None:
                match = re.match(r'^(\d+)(.*)$', wheel.build_tag)
                build_tag_groups = match.groups()
                build_tag = (int(build_tag_groups[0]), build_tag_groups[1])
        else:  # sdist
            pri = -(support_num)
        return (binary_preference, candidate.version, build_tag, pri)

    def get_best_candidate(self, candidates):
        # type: (List[InstallationCandidate]) -> InstallationCandidate
        """
        Return the best candidate per the instance's sort order, or None if
        no candidates are given.
        """
        if not candidates:
            return None

        return max(candidates, key=self._sort_key)


class FoundCandidates(object):
    """A collection of candidates, returned by `PackageFinder.find_candidates`.

    This class is only intended to be instantiated by CandidateEvaluator's
    `make_found_candidates()` method.
    """

    def __init__(
        self,
        candidates,     # type: List[InstallationCandidate]
        versions,       # type: Set[str]
        evaluator,      # type: CandidateEvaluator
    ):
        # type: (...) -> None
        """
        :param candidates: A sequence of all available candidates found.
        :param versions: The applicable versions to filter applicable
            candidates.
        :param evaluator: A CandidateEvaluator object to sort applicable
            candidates by order of preference.
        """
        self._candidates = candidates
        self._evaluator = evaluator
        self._versions = versions

    def iter_all(self):
        # type: () -> Iterable[InstallationCandidate]
        """Iterate through all candidates.
        """
        return iter(self._candidates)

    def iter_applicable(self):
        # type: () -> Iterable[InstallationCandidate]
        """Iterate through candidates matching the versions associated with
        this instance.
        """
        # Again, converting version to str to deal with debundling.
        return (c for c in self.iter_all() if str(c.version) in self._versions)

    def get_best(self):
        # type: () -> Optional[InstallationCandidate]
        """Return the best candidate available, or None if no applicable
        candidates are found.
        """
        candidates = list(self.iter_applicable())
        return self._evaluator.get_best_candidate(candidates)


class PackageFinder(object):
    """This finds packages.

    This is meant to match easy_install's technique for looking for
    packages, by reading pages and looking for appropriate links.
    """

    def __init__(
        self,
        candidate_evaluator,  # type: CandidateEvaluator
        search_scope,         # type: SearchScope
        session,  # type: PipSession
        format_control=None,  # type: Optional[FormatControl]
        trusted_hosts=None,   # type: Optional[List[str]]
    ):
        # type: (...) -> None
        """
        This constructor is primarily meant to be used by the create() class
        method and from tests.

        :param candidate_evaluator: A CandidateEvaluator object.
        :param session: The Session to use to make requests.
        :param format_control: A FormatControl object, used to control
            the selection of source packages / binary packages when consulting
            the index and links.
        """
        if trusted_hosts is None:
            trusted_hosts = []

        format_control = format_control or FormatControl(set(), set())

        self.candidate_evaluator = candidate_evaluator
        self.search_scope = search_scope
        self.session = session
        self.format_control = format_control
        self.trusted_hosts = trusted_hosts

        # These are boring links that have already been logged somehow.
        self._logged_links = set()  # type: Set[Link]

    @classmethod
    def create(
        cls,
        find_links,  # type: List[str]
        index_urls,  # type: List[str]
        allow_all_prereleases=False,  # type: bool
        trusted_hosts=None,  # type: Optional[List[str]]
        session=None,  # type: Optional[PipSession]
        format_control=None,  # type: Optional[FormatControl]
        target_python=None,  # type: Optional[TargetPython]
        prefer_binary=False,  # type: bool
        ignore_requires_python=None,  # type: Optional[bool]
    ):
        # type: (...) -> PackageFinder
        """Create a PackageFinder.

        :param trusted_hosts: Domains not to emit warnings for when not using
            HTTPS.
        :param session: The Session to use to make requests.
        :param format_control: A FormatControl object or None. Used to control
            the selection of source packages / binary packages when consulting
            the index and links.
        :param target_python: The target Python interpreter.
        :param prefer_binary: Whether to prefer an old, but valid, binary
            dist over a new source dist.
        :param ignore_requires_python: Whether to ignore incompatible
            "Requires-Python" values in links. Defaults to False.
        """
        if session is None:
            raise TypeError(
                "PackageFinder.create() missing 1 required keyword argument: "
                "'session'"
            )

        search_scope = SearchScope.create(
            find_links=find_links,
            index_urls=index_urls,
        )

        candidate_evaluator = CandidateEvaluator(
            target_python=target_python,
            prefer_binary=prefer_binary,
            allow_all_prereleases=allow_all_prereleases,
            ignore_requires_python=ignore_requires_python,
        )

        return cls(
            candidate_evaluator=candidate_evaluator,
            search_scope=search_scope,
            session=session,
            format_control=format_control,
            trusted_hosts=trusted_hosts,
        )

    @property
    def find_links(self):
        # type: () -> List[str]
        return self.search_scope.find_links

    @property
    def index_urls(self):
        # type: () -> List[str]
        return self.search_scope.index_urls

    @property
    def allow_all_prereleases(self):
        # type: () -> bool
        return self.candidate_evaluator.allow_all_prereleases

    def set_allow_all_prereleases(self):
        # type: () -> None
        self.candidate_evaluator.allow_all_prereleases = True

    def add_trusted_host(self, host, source=None):
        # type: (str, Optional[str]) -> None
        """
        :param source: An optional source string, for logging where the host
            string came from.
        """
        # It is okay to add a previously added host because PipSession stores
        # the resulting prefixes in a dict.
        msg = 'adding trusted host: {!r}'.format(host)
        if source is not None:
            msg += ' (from {})'.format(source)
        logger.info(msg)
        self.session.add_insecure_host(host)
        if host in self.trusted_hosts:
            return

        self.trusted_hosts.append(host)

    def iter_secure_origins(self):
        # type: () -> Iterator[SecureOrigin]
        for secure_origin in SECURE_ORIGINS:
            yield secure_origin
        for host in self.trusted_hosts:
            yield ('*', host, '*')

    @staticmethod
    def _sort_locations(locations, expand_dir=False):
        # type: (Sequence[str], bool) -> Tuple[List[str], List[str]]
        """
        Sort locations into "files" (archives) and "urls", and return
        a pair of lists (files,urls)
        """
        files = []
        urls = []

        # puts the url for the given file path into the appropriate list
        def sort_path(path):
            url = path_to_url(path)
            if mimetypes.guess_type(url, strict=False)[0] == 'text/html':
                urls.append(url)
            else:
                files.append(url)

        for url in locations:

            is_local_path = os.path.exists(url)
            is_file_url = url.startswith('file:')

            if is_local_path or is_file_url:
                if is_local_path:
                    path = url
                else:
                    path = url_to_path(url)
                if os.path.isdir(path):
                    if expand_dir:
                        path = os.path.realpath(path)
                        for item in os.listdir(path):
                            sort_path(os.path.join(path, item))
                    elif is_file_url:
                        urls.append(url)
                    else:
                        logger.warning(
                            "Path '{0}' is ignored: "
                            "it is a directory.".format(path),
                        )
                elif os.path.isfile(path):
                    sort_path(path)
                else:
                    logger.warning(
                        "Url '%s' is ignored: it is neither a file "
                        "nor a directory.", url,
                    )
            elif is_url(url):
                # Only add url with clear scheme
                urls.append(url)
            else:
                logger.warning(
                    "Url '%s' is ignored. It is either a non-existing "
                    "path or lacks a specific scheme.", url,
                )

        return files, urls

    def _validate_secure_origin(self, logger, location):
        # type: (Logger, Link) -> bool
        # Determine if this url used a secure transport mechanism
        parsed = urllib_parse.urlparse(str(location))
        origin = (parsed.scheme, parsed.hostname, parsed.port)

        # The protocol to use to see if the protocol matches.
        # Don't count the repository type as part of the protocol: in
        # cases such as "git+ssh", only use "ssh". (I.e., Only verify against
        # the last scheme.)
        protocol = origin[0].rsplit('+', 1)[-1]

        # Determine if our origin is a secure origin by looking through our
        # hardcoded list of secure origins, as well as any additional ones
        # configured on this PackageFinder instance.
        for secure_origin in self.iter_secure_origins():
            if protocol != secure_origin[0] and secure_origin[0] != "*":
                continue

            try:
                # We need to do this decode dance to ensure that we have a
                # unicode object, even on Python 2.x.
                addr = ipaddress.ip_address(
                    origin[1]
                    if (
                        isinstance(origin[1], six.text_type) or
                        origin[1] is None
                    )
                    else origin[1].decode("utf8")
                )
                network = ipaddress.ip_network(
                    secure_origin[1]
                    if isinstance(secure_origin[1], six.text_type)
                    # setting secure_origin[1] to proper Union[bytes, str]
                    # creates problems in other places
                    else secure_origin[1].decode("utf8")  # type: ignore
                )
            except ValueError:
                # We don't have both a valid address or a valid network, so
                # we'll check this origin against hostnames.
                if (origin[1] and
                        origin[1].lower() != secure_origin[1].lower() and
                        secure_origin[1] != "*"):
                    continue
            else:
                # We have a valid address and network, so see if the address
                # is contained within the network.
                if addr not in network:
                    continue

            # Check to see if the port patches
            if (origin[2] != secure_origin[2] and
                    secure_origin[2] != "*" and
                    secure_origin[2] is not None):
                continue

            # If we've gotten here, then this origin matches the current
            # secure origin and we should return True
            return True

        # If we've gotten to this point, then the origin isn't secure and we
        # will not accept it as a valid location to search. We will however
        # log a warning that we are ignoring it.
        logger.warning(
            "The repository located at %s is not a trusted or secure host and "
            "is being ignored. If this repository is available via HTTPS we "
            "recommend you use HTTPS instead, otherwise you may silence "
            "this warning and allow it anyway with '--trusted-host %s'.",
            parsed.hostname,
            parsed.hostname,
        )

        return False

    def find_all_candidates(self, project_name):
        # type: (str) -> List[InstallationCandidate]
        """Find all available InstallationCandidate for project_name

        This checks index_urls and find_links.
        All versions found are returned as an InstallationCandidate list.

        See CandidateEvaluator.evaluate_link() for details on which files
        are accepted.
        """
        search_scope = self.search_scope
        index_locations = search_scope.get_index_urls_locations(project_name)
        index_file_loc, index_url_loc = self._sort_locations(index_locations)
        fl_file_loc, fl_url_loc = self._sort_locations(
            self.find_links, expand_dir=True,
        )

        file_locations = (Link(url) for url in itertools.chain(
            index_file_loc, fl_file_loc,
        ))

        # We trust every url that the user has given us whether it was given
        #   via --index-url or --find-links.
        # We want to filter out any thing which does not have a secure origin.
        url_locations = [
            link for link in itertools.chain(
                (Link(url) for url in index_url_loc),
                (Link(url) for url in fl_url_loc),
            )
            if self._validate_secure_origin(logger, link)
        ]

        logger.debug('%d location(s) to search for versions of %s:',
                     len(url_locations), project_name)

        for location in url_locations:
            logger.debug('* %s', location)

        canonical_name = canonicalize_name(project_name)
        formats = self.format_control.get_allowed_formats(canonical_name)
        search = Search(project_name, canonical_name, formats)
        find_links_versions = self._package_versions(
            # We trust every directly linked archive in find_links
            (Link(url, '-f') for url in self.find_links),
            search
        )

        page_versions = []
        for page in self._get_pages(url_locations, project_name):
            logger.debug('Analyzing links from page %s', page.url)
            with indent_log():
                page_versions.extend(
                    self._package_versions(page.iter_links(), search)
                )

        file_versions = self._package_versions(file_locations, search)
        if file_versions:
            file_versions.sort(reverse=True)
            logger.debug(
                'Local files found: %s',
                ', '.join([
                    url_to_path(candidate.location.url)
                    for candidate in file_versions
                ])
            )

        # This is an intentional priority ordering
        return file_versions + find_links_versions + page_versions

    def find_candidates(
        self,
        project_name,       # type: str
        specifier=None,     # type: Optional[specifiers.BaseSpecifier]
    ):
        # type: (...) -> FoundCandidates
        """Find matches for the given project and specifier.

        :param specifier: An optional object implementing `filter`
            (e.g. `packaging.specifiers.SpecifierSet`) to filter applicable
            versions.

        :return: A `FoundCandidates` instance.
        """
        candidates = self.find_all_candidates(project_name)
        return self.candidate_evaluator.make_found_candidates(
            candidates, specifier=specifier,
        )

    def find_requirement(self, req, upgrade):
        # type: (InstallRequirement, bool) -> Optional[Link]
        """Try to find a Link matching req

        Expects req, an InstallRequirement and upgrade, a boolean
        Returns a Link if found,
        Raises DistributionNotFound or BestVersionAlreadyInstalled otherwise
        """
        candidates = self.find_candidates(req.name, req.specifier)
        best_candidate = candidates.get_best()

        installed_version = None    # type: Optional[_BaseVersion]
        if req.satisfied_by is not None:
            installed_version = parse_version(req.satisfied_by.version)

        def _format_versions(cand_iter):
            # This repeated parse_version and str() conversion is needed to
            # handle different vendoring sources from pip and pkg_resources.
            # If we stop using the pkg_resources provided specifier and start
            # using our own, we can drop the cast to str().
            return ", ".join(sorted(
                {str(c.version) for c in cand_iter},
                key=parse_version,
            )) or "none"

        if installed_version is None and best_candidate is None:
            logger.critical(
                'Could not find a version that satisfies the requirement %s '
                '(from versions: %s)',
                req,
                _format_versions(candidates.iter_all()),
            )

            raise DistributionNotFound(
                'No matching distribution found for %s' % req
            )

        best_installed = False
        if installed_version and (
                best_candidate is None or
                best_candidate.version <= installed_version):
            best_installed = True

        if not upgrade and installed_version is not None:
            if best_installed:
                logger.debug(
                    'Existing installed version (%s) is most up-to-date and '
                    'satisfies requirement',
                    installed_version,
                )
            else:
                logger.debug(
                    'Existing installed version (%s) satisfies requirement '
                    '(most up-to-date version is %s)',
                    installed_version,
                    best_candidate.version,
                )
            return None

        if best_installed:
            # We have an existing version, and its the best version
            logger.debug(
                'Installed version (%s) is most up-to-date (past versions: '
                '%s)',
                installed_version,
                _format_versions(candidates.iter_applicable()),
            )
            raise BestVersionAlreadyInstalled

        logger.debug(
            'Using version %s (newest of versions: %s)',
            best_candidate.version,
            _format_versions(candidates.iter_applicable()),
        )
        return best_candidate.location

    def _get_pages(self, locations, project_name):
        # type: (Iterable[Link], str) -> Iterable[HTMLPage]
        """
        Yields (page, page_url) from the given locations, skipping
        locations that have errors.
        """
        seen = set()  # type: Set[Link]
        for location in locations:
            if location in seen:
                continue
            seen.add(location)

            page = _get_html_page(location, session=self.session)
            if page is None:
                continue

            yield page

    def _sort_links(self, links):
        # type: (Iterable[Link]) -> List[Link]
        """
        Returns elements of links in order, non-egg links first, egg links
        second, while eliminating duplicates
        """
        eggs, no_eggs = [], []
        seen = set()  # type: Set[Link]
        for link in links:
            if link not in seen:
                seen.add(link)
                if link.egg_fragment:
                    eggs.append(link)
                else:
                    no_eggs.append(link)
        return no_eggs + eggs

    def _log_skipped_link(self, link, reason):
        # type: (Link, str) -> None
        if link not in self._logged_links:
            # Put the link at the end so the reason is more visible and
            # because the link string is usually very long.
            logger.debug('Skipping link: %s: %s', reason, link)
            self._logged_links.add(link)

    def get_install_candidate(self, link, search):
        # type: (Link, Search) -> Optional[InstallationCandidate]
        """
        If the link is a candidate for install, convert it to an
        InstallationCandidate and return it. Otherwise, return None.
        """
        is_candidate, result = (
            self.candidate_evaluator.evaluate_link(link, search=search)
        )
        if not is_candidate:
            if result:
                self._log_skipped_link(link, reason=result)
            return None

        return InstallationCandidate(
            search.supplied, location=link, version=result,
        )

    def _package_versions(
        self,
        links,  # type: Iterable[Link]
        search  # type: Search
    ):
        # type: (...) -> List[InstallationCandidate]
        result = []
        for link in self._sort_links(links):
            candidate = self.get_install_candidate(link, search=search)
            if candidate is not None:
                result.append(candidate)
        return result


def _find_name_version_sep(egg_info, canonical_name):
    # type: (str, str) -> int
    """Find the separator's index based on the package's canonical name.

    `egg_info` must be an egg info string for the given package, and
    `canonical_name` must be the package's canonical name.

    This function is needed since the canonicalized name does not necessarily
    have the same length as the egg info's name part. An example::

    >>> egg_info = 'foo__bar-1.0'
    >>> canonical_name = 'foo-bar'
    >>> _find_name_version_sep(egg_info, canonical_name)
    8
    """
    # Project name and version must be separated by one single dash. Find all
    # occurrences of dashes; if the string in front of it matches the canonical
    # name, this is the one separating the name and version parts.
    for i, c in enumerate(egg_info):
        if c != "-":
            continue
        if canonicalize_name(egg_info[:i]) == canonical_name:
            return i
    raise ValueError("{} does not match {}".format(egg_info, canonical_name))


def _egg_info_matches(egg_info, canonical_name):
    # type: (str, str) -> Optional[str]
    """Pull the version part out of a string.

    :param egg_info: The string to parse. E.g. foo-2.1
    :param canonical_name: The canonicalized name of the package this
        belongs to.
    """
    try:
        version_start = _find_name_version_sep(egg_info, canonical_name) + 1
    except ValueError:
        return None
    version = egg_info[version_start:]
    if not version:
        return None
    return version


def _determine_base_url(document, page_url):
    """Determine the HTML document's base URL.

    This looks for a ``<base>`` tag in the HTML document. If present, its href
    attribute denotes the base URL of anchor tags in the document. If there is
    no such tag (or if it does not have a valid href attribute), the HTML
    file's URL is used as the base URL.

    :param document: An HTML document representation. The current
        implementation expects the result of ``html5lib.parse()``.
    :param page_url: The URL of the HTML document.
    """
    for base in document.findall(".//base"):
        href = base.get("href")
        if href is not None:
            return href
    return page_url


def _get_encoding_from_headers(headers):
    """Determine if we have any encoding information in our headers.
    """
    if headers and "Content-Type" in headers:
        content_type, params = cgi.parse_header(headers["Content-Type"])
        if "charset" in params:
            return params['charset']
    return None


def _clean_link(url):
    # type: (str) -> str
    """Makes sure a link is fully encoded.  That is, if a ' ' shows up in
    the link, it will be rewritten to %20 (while not over-quoting
    % or other characters)."""
    # Split the URL into parts according to the general structure
    # `scheme://netloc/path;parameters?query#fragment`. Note that the
    # `netloc` can be empty and the URI will then refer to a local
    # filesystem path.
    result = urllib_parse.urlparse(url)
    # In both cases below we unquote prior to quoting to make sure
    # nothing is double quoted.
    if result.netloc == "":
        # On Windows the path part might contain a drive letter which
        # should not be quoted. On Linux where drive letters do not
        # exist, the colon should be quoted. We rely on urllib.request
        # to do the right thing here.
        path = urllib_request.pathname2url(
            urllib_request.url2pathname(result.path))
    else:
        # In addition to the `/` character we protect `@` so that
        # revision strings in VCS URLs are properly parsed.
        path = urllib_parse.quote(urllib_parse.unquote(result.path), safe="/@")
    return urllib_parse.urlunparse(result._replace(path=path))


class HTMLPage(object):
    """Represents one page, along with its URL"""

    def __init__(self, content, url, headers=None):
        # type: (bytes, str, MutableMapping[str, str]) -> None
        self.content = content
        self.url = url
        self.headers = headers

    def __str__(self):
        return redact_password_from_url(self.url)

    def iter_links(self):
        # type: () -> Iterable[Link]
        """Yields all links in the page"""
        document = html5lib.parse(
            self.content,
            transport_encoding=_get_encoding_from_headers(self.headers),
            namespaceHTMLElements=False,
        )
        base_url = _determine_base_url(document, self.url)
        for anchor in document.findall(".//a"):
            if anchor.get("href"):
                href = anchor.get("href")
                url = _clean_link(urllib_parse.urljoin(base_url, href))
                pyrequire = anchor.get('data-requires-python')
                pyrequire = unescape(pyrequire) if pyrequire else None
                yield Link(url, self.url, requires_python=pyrequire)


Search = namedtuple('Search', 'supplied canonical formats')
"""Capture key aspects of a search.

:attribute supplied: The user supplied package.
:attribute canonical: The canonical package name.
:attribute formats: The formats allowed for this package. Should be a set
    with 'binary' or 'source' or both in it.
"""
