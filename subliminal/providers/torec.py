# -*- coding: utf-8 -*-
import enum
import io
import logging
import random
import re
import zipfile

from babelfish import Language
from guessit import guessit
from requests import Session, Response

from . import ParserBeautifulSoup, Provider
from .. import __short_version__
from ..exceptions import AuthenticationError, ConfigurationError, ProviderError
from ..subtitle import Subtitle, fix_line_ending, guess_matches
from ..utils import sanitize
from ..video import Episode, Movie

logger = logging.getLogger(__name__)


class TorecMediaTypes(enum.Enum):
    SERIES_WITH_SUBTITLES = 3
    MOVIE_WITHOUT_SUBTITLES = 7
    MOVIE_WITH_SUBTITLES = 10


class TorecSubtitle(Subtitle):
    """Torec Subtitle."""
    provider_name = 'torec'

    def __init__(self, language, hearing_impaired, page_link, series, season, episode, title, subtitle_id,
                 releases_to_dl_codes_mapping):

        super(TorecSubtitle, self).__init__(language, hearing_impaired, page_link)
        self.series = series
        self.season = season
        self.episode = episode
        self.title = title
        self.subtitle_id = subtitle_id
        self.releases_to_dl_codes_mapping = releases_to_dl_codes_mapping

    @property
    def id(self):
        return str(self.subtitle_id)

    def get_matches(self, video):
        matches = set()

        # episode
        if isinstance(video, Episode):
            # series
            if video.series and sanitize(self.series) == sanitize(video.series):
                matches.add('series')
            # season
            if video.season and self.season == video.season:
                matches.add('season')
            # episode
            if video.episode and self.episode == video.episode:
                matches.add('episode')
            # guess
            for release in self.releases_to_dl_codes_mapping:
                matches |= guess_matches(video, guessit(release, {'type': 'episode'}))
        # movie
        elif isinstance(video, Movie):
            # guess
            for release in self.releases_to_dl_codes_mapping:
                matches |= guess_matches(video, guessit(release, {'type': 'movie'}))

        # title
        if video.title and sanitize(self.title) == sanitize(video.title):
            matches.add('title')

        return matches


class TorecProvider(Provider):
    """Torec Provider."""
    languages = {Language.fromalpha2(l) for l in ['he']}
    server_url = 'http://torec.net/'

    def __init__(self, username=None, password=None):
        if username is not None and password is None or username is None and password is not None:
            raise ConfigurationError('Username and password must be specified')

        self.session = None  # type: Session
        self.username = username
        self.password = password
        self.logged_in = False

    def initialize(self):
        self.session = Session()
        self.session.headers['User-Agent'] = 'Subliminal/{}'.format(__short_version__)

        # login
        if self.username is not None and self.password is not None:
            logger.debug('Logging in')
            url = self.server_url + 'ajax/login/t7/loginProcess.asp?rnd={}'.format(random.random())

            # actual login
            data = {'form': 'true', 'username': self.username, 'password': self.password}
            r = self.session.post(url, data, allow_redirects=False, timeout=10)

            # The site will return 200 but error code in content
            if r.content == b'1':
                raise AuthenticationError(self.username)

            logger.info('Logged in')
            self.logged_in = True

    def terminate(self):
        # logout
        if self.logged_in:
            logger.info('Logging out')
            r = self.session.get(self.server_url + 'ajax/login/t7/logout.asp?redirected=true', timeout=10)
            r.raise_for_status()
            logger.info('Logged out')
            self.logged_in = False

        self.session.close()

    def _search_url_titles(self, title):
        """Search the URL titles by kind for the given `title`.

        :param str title: title to search for.
        :return: the URL titles by kind.
        :rtype: collections.defaultdict

        """
        # hit the autocompletion json api
        logger.info('Searching id for {}'.format(title))

        # requests is being annoying with escaping of the quotes that subliminal uses and the site is very sensitive to this.
        url_base = self.server_url + 'ajax/search/acSearch.asp'
        r = self.session.get(url_base, timeout=10, params={'query': title.replace('\'', ' ')})

        r.raise_for_status()
        suggestions = r.json()

        return suggestions['suggestions']

    def _get_episode_subtitles_from_series_page(self, series_url=None, season=None, episode=None):
        series_page = self.session.get(series_url)  # type: Response
        soup = ParserBeautifulSoup(series_page.content, ['html5lib', 'lxml', 'html.parser'])
        seasons_tabs = soup.find_all(attrs={'id': re.compile('tabs4-\w+')})
        if not seasons_tabs:
            return {}

        episodes_re = re.compile('(\d+)-?(\d+)?')
        subtitles = {}
        for season_index, s in enumerate(seasons_tabs):
            episodes = {}
            for a in s.find_all('a'):
                url = a['href']
                match = episodes_re.findall(a.text)
                if match:
                    start, end = match[0]
                    if end:
                        for episode_indes in range(int(start), int(end) + 1):
                            episodes[episode_indes] = url
                    else:
                        episodes[start] = url

            # 1-based count
            subtitles[season_index + 1] = episodes

        wanted_subtitle = subtitles.get(season, {}).get(episode)

        return wanted_subtitle

    def query(self, title, year=None, season=None, episode=None):
        # search for the url title
        url_titles = self._search_url_titles(title)

        if not url_titles:
            logger.error('No URL title found for {}'.format(title))
            return []

        url_title = None
        if season and episode:
            matching_urls = [i for i in url_titles if i['type'] == TorecMediaTypes.SERIES_WITH_SUBTITLES.value]

            if not matching_urls:
                logger.error('No URL title found for series {}'.format(title))
                return []

            if year:
                for title in matching_urls:
                    url = self.server_url + title['data']
                    r = self.session.get(url)
                    soup = ParserBeautifulSoup(r.content, ['html5lib', 'lxml', 'html.parser'])

                    # sadly this is the only way to access year data..
                    year_div = soup.select('body > section > div.col-xs-9.col-sm-9.col-md-9.col-lg-9.subDetails > h5')
                    series_year_start, series_year_end = re.findall('(\d{4})?-?(\d{4})', year_div[0].text)[0]
                    if series_year_end:
                        if series_year_start <= year <= series_year_end:
                            url_title = url
                            break
                    else:
                        if series_year_start <= year:
                            url_title = url
                            break

                if not url_title:
                    logger.error('No URL title found for series {}'.format(title))
                    return []
            else:
                url_title = self.server_url + matching_urls[0]['data']

            logger.debug('Using series title %r', url_title)
            wanted_episode_url_or_none = self._get_episode_subtitles_from_series_page(url_title, season=season,
                                                                                      episode=episode)
            if not wanted_episode_url_or_none:
                return []

            url = self.server_url + wanted_episode_url_or_none

        else:
            matching_urls = [i for i in url_titles if i['type'] == TorecMediaTypes.MOVIE_WITH_SUBTITLES.value]

            if not matching_urls:
                logger.error('No URL title found for movie {}'.format(title))
                return []

            if year:
                for title in matching_urls:

                    url = self.server_url + title['data']
                    r = self.session.get(url)
                    soup = ParserBeautifulSoup(r.content, ['html5lib', 'lxml', 'html.parser'])

                    # sadly this is the only way to access year data..
                    movie_year_css_selector = "body > section > div.siteTourSubDetails > div.col-xs-9.col-sm-9.col-md-9.col-lg-9.subDetails > h2 > span"

                    year_div = soup.select(movie_year_css_selector)
                    movie_year = re.findall('(\d+)', year_div[0].text)[0]
                    if movie_year and year == movie_year:
                        url_title = url
                        break
                if not url_title:
                    logger.error('No URL title found for series {}'.format(title))
                    return []
            else:
                url_title = self.server_url + matching_urls[0]['data']

            logger.debug('Using movie title %r', url_title)

            # movies subs don't need any further resolving
            url = url_title

        # get the list of subtitles
        logger.debug('Getting the list of subtitles')
        r = self.session.get(url)
        r.raise_for_status()

        soup = ParserBeautifulSoup(r.content, ['html5lib', 'lxml', 'html.parser'])
        subtitle_id = re.match('.+sub_id=(\d+)', url).group()[0]

        # this code is used when querying the server for download token for each subtitle
        dl_code_regex = re.compile('dlRow_(.+)')
        rows = soup.find_all(attrs={'id': dl_code_regex})

        release_to_dl_codes_mapping = {}
        for i, row in enumerate(rows):
            release = row.find_next(attrs={'class': 'version'}).text
            dl_code = dl_code_regex.match(row['id']).groups()[0]
            release_to_dl_codes_mapping[release] = dl_code

        # TODO: decide how to split subtitles so it will be more convenient to select release
        subtitle = TorecSubtitle(language=Language.fromalpha2('he'),
                                 hearing_impaired=False,
                                 page_link=url,
                                 series=title,
                                 season=season,
                                 episode=episode,
                                 title=title,
                                 subtitle_id=subtitle_id,
                                 releases_to_dl_codes_mapping=release_to_dl_codes_mapping)

        logger.debug('Found subtitle {}'.format(subtitle))

        return [subtitle]

    def list_subtitles(self, video, languages):
        season = episode = None
        title = video.title

        if isinstance(video, Episode):
            title = video.series
            season = video.season
            episode = video.episode

        return [s for s in self.query(title, season, episode) if s.language in languages]

    # TODO: implement
    def download_subtitle(self, subtitle):
        pass
        # url = self.server_url + 'subtitle/download/{}/{}/'.format(subtitle.language.alpha2, subtitle.subtitle_id)
        # params = {'v': subtitle.releases[0], 'key': subtitle.subtitle_key}
        # r = self.session.get(url, params=params, headers={'Referer': subtitle.page_link}, timeout=10)
        # r.raise_for_status()

        # open the zip
        # with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        #     # remove some filenames from the namelist
        #     namelist = [n for n in zf.namelist() if not n.endswith('.txt')]
        #     if len(namelist) > 1:
        #         raise ProviderError('More than one file to unzip')
        #
        #     subtitle.content = fix_line_ending(zf.read(namelist[0]))
