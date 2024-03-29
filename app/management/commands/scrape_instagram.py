from django.core.management.base import BaseCommand, CommandError
from app.models import InstagramUser, InstagramBot
from app.management.commands.instagram.constants import *

import argparse
import codecs
import configparser
import errno
import glob
from operator import itemgetter
import json
import logging.config
import os
import re
import sys
import textwrap
import time

try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

import warnings
import threading
import concurrent.futures
import requests
import tqdm
DESTINATION = "./data/"
class Struct:
    def __init__(self, **entries):
        self.__dict__.update(entries)

try:
    reload(sys)  # Python 2.7
    sys.setdefaultencoding("UTF8")
except NameError:
    pass

warnings.filterwarnings('ignore')

input_lock = threading.RLock()

class LockedStream(object):
    file = None
    def __init__(self, file):
        self.file = file

    def write(self, x):
        with input_lock:
            self.file.write(x)

    def flush(self):
        return getattr(self.file, 'flush', lambda: None)()

original_stdout, original_stderr = sys.stdout, sys.stderr
sys.stdout, sys.stderr = map(LockedStream, (sys.stdout, sys.stderr))

def threaded_input(prompt):
    with input_lock:
        try:
            with tqdm.external_write_mode():
                original_stdout.write(prompt)
                original_stdout.flush()
                return sys.stdin.readline()
        except AttributeError:
            original_stdout.write('\n')
            original_stdout.write(prompt)
            original_stdout.flush()
            return sys.stdin.readline()

input = threaded_input

class PartialContentException(Exception):
    pass

class Unfollow(object):
    '''
    Mixin to implement unfollowing functionality
    '''
    def compute_unfollowers(self):
        follower_ids = set([follower['id'] for follower in self._followers])
        following_ids = set([following['id'] for following in self._following])
        unfollowers = following_ids - follower_ids
        print "Number of unfollowers", len(unfollowers)
        return unfollowers

    def unfollow_user(self, id):
        print "Unfollowing", id
        '''
        You can only unfollow up to 15 people every 5 minutes
        '''
        self.safe_post(UNFOLLOW_URL.format(id))

    def perform_bulk_unfollow(self):
        for idx, unfollower in enumerate(self.compute_unfollowers()):
            if idx and idx % 14 == 0:
                self.sleep(60 * 16)
            self.unfollow_user(unfollower)

class BulkFollow(object):
    '''
    Mixin to implement followign functionality
    '''

    def follow_user(self, id):
        print "Following", id
        self.safe_post(FOLLOW_URL.format(id))

    def perform_bulk_follow(self):
        assert type(self.bulk_follow) is str, "Argument to bulk_follow must be file path"
        currently_following = set([user['username'] for user in self._following])
        try:
            to_follow = set([str(line.strip()) for line in open(self.bulk_follow)])
        except Exception as e:
            raise ValueError("Could not open file %s. Error: %s" % (self.bulk_follow, e))

        print "Currently following", len(currently_following)
        print "Should be following", len(to_follow)
        print "Need to follow", len(to_follow - currently_following)

        not_following = to_follow - currently_following

        for username in not_following:
            # print "Getting", username
            try:
                user = self.get_user(username)
                self.follow_user(user['id'])
            except Exception as e:
                print e

        raise SystemExit
class InstagramScraper(Unfollow, BulkFollow):
    """InstagramScraper scrapes and downloads an instagram user's photos and videos"""

    def __init__(self, **kwargs):
        # define a set of default attributes
        default_attr = dict(username='', usernames=[], filename=None,
                            login_user=None, login_pass=None, login_only=False,
                            # whether to scrape the followers or users they're following
                            followers=False, following=False,
                            # custom following functionality
                            unfollow=False, bulk_follow=None,
                            # ignore the images (just scrape followers)
                            no_media=False,
                            destination=DESTINATION, retain_username=False, interactive=False,
                            quiet=False, maximum=0, media_metadata=False, latest=False,
                            latest_stamps=False,
                            media_types=['image', 'video', 'story-image', 'story-video'],
                            tag=False, location=False, search_location=False, comments=False,
                            verbose=0, include_location=False, filter=None)

        allowed_attr = list(default_attr.keys())
        # update those defaults with whatever arguments are passed to
        # this class
        default_attr.update(kwargs)

        # iterate through all of the attributes
        for key in default_attr:
            if key in allowed_attr:
                # attach it to this object
                self.__dict__[key] = kwargs.get(key)

        # story media type means story-image & story-video
        if 'story' in self.media_types:
            self.media_types.remove('story')
            if 'story-image' not in self.media_types:
                self.media_types.append('story-image')
            if 'story-video' not in self.media_types:
                self.media_types.append('story-video')

        # Read latest_stamps file with ConfigParser
        self.latest_stamps_parser = None
        if self.latest_stamps:
            parser = configparser.ConfigParser()
            parser.read(self.latest_stamps)
            self.latest_stamps_parser = parser
            # If we have a latest_stamps file, latest must be true as it's the common flag
            self.latest = True

        # Set up a logger
        self.logger = InstagramScraper.get_logger(level=logging.DEBUG, verbose=default_attr.get('verbose'))

        self.posts = []
        self.session = requests.Session()
        self.session.headers = {'user-agent': CHROME_WIN_UA}

        self.cookies = None
        self.logged_in = False
        self.last_scraped_filemtime = 0
        if default_attr['filter']:
            self.filter = list(self.filter)

    def sleep(self, secs):
        min_delay = 1
        for _ in range(secs // min_delay):
            time.sleep(min_delay)
            if self.quit:
                return
        time.sleep(secs % min_delay)

    def _retry_prompt(self, url, exception_message):
        """Show prompt and return True: retry, False: ignore, None: abort"""
        answer = input( 'Repeated error {0}\n(A)bort, (I)gnore, (R)etry or retry (F)orever?'.format(exception_message) )
        if answer:
            answer = answer[0].upper()
            if answer == 'I':
                self.logger.info( 'The user has chosen to ignore {0}'.format(url) )
                return False
            elif answer == 'R':
                return True
            elif answer == 'F':
                self.logger.info( 'The user has chosen to retry forever' )
                global MAX_RETRIES
                MAX_RETRIES = sys.maxsize
                return True
            else:
                self.logger.info( 'The user has chosen to abort' )
                return None

    def safe_post(self, *args, **kwargs):
        retry = 0
        retry_delay = RETRY_DELAY

        while True:
            if self.quit:
                return
            try:
                response = self.session.post(timeout=CONNECT_TIMEOUT, *args, **kwargs)
                if response.status_code == 404:
                    return
                response.raise_for_status()
                return response
            except:
                raise



    def safe_get(self, *args, **kwargs):
        # out of the box solution
        # session.mount('https://', HTTPAdapter(max_retries=...))
        # only covers failed DNS lookups, socket connections and connection timeouts
        # It doesnt work when server terminate connection while response is downloaded
        retry = 0
        retry_delay = RETRY_DELAY
        while True:
            if self.quit:
                return
            try:
                response = self.session.get(timeout=CONNECT_TIMEOUT, *args, **kwargs)
                if response.status_code == 404:
                    return
                response.raise_for_status()
                content_length = response.headers.get('Content-Length')
                if content_length is None or len(response.content) != int(content_length):
                    #if content_length is None we repeat anyway to get size and be confident
                    raise PartialContentException('Partial response')
                return response
            except (KeyboardInterrupt):
                raise
            except (requests.exceptions.RequestException, PartialContentException) as e:
                if 'url' in kwargs:
                    url = kwargs['url']
                elif len(args) > 0:
                    url = args[0]
                if retry < MAX_RETRIES:
                    self.logger.warning('Retry after exception {0} on {1}'.format(repr(e), url))
                    self.sleep(retry_delay)
                    retry_delay = min( 2 * retry_delay, MAX_RETRY_DELAY )
                    retry = retry + 1
                    continue
                else:
                    keep_trying = self._retry_prompt(url, repr(e))
                    if keep_trying == True:
                        retry = 0
                        continue
                    elif keep_trying == False:
                        return
                raise

    def get_json(self, *args, **kwargs):
        """Retrieve text from url. Return text as string or None if no data present """
        resp = self.safe_get(*args, **kwargs)
        if resp is not None:
            return resp.text

    def login(self):
        """Logs in to instagram."""
        self.session.headers.update({'Referer': BASE_URL})
        req = self.session.get(BASE_URL)

        self.session.headers.update({'X-CSRFToken': req.cookies['csrftoken']})

        login_data = {'username': self.login_user, 'password': self.login_pass}
        login = self.session.post(LOGIN_URL, data=login_data, allow_redirects=True)
        self.session.headers.update({'X-CSRFToken': login.cookies['csrftoken']})
        self.cookies = login.cookies
        login_text = json.loads(login.text)

        if login_text.get('authenticated') and login.status_code == 200:
            self.logged_in = True
        else:
            self.logger.error('Login failed for ' + self.login_user)

            if 'checkpoint_url' in login_text:
                checkpoint_url = login_text.get('checkpoint_url')
                self.logger.error('Please verify your account at ' + checkpoint_url)

                if self.interactive is True:
                    self.login_challenge(checkpoint_url)
            elif 'errors' in login_text:
                for count, error in enumerate(login_text['errors'].get('error')):
                    count += 1
                    self.logger.debug('Session error %(count)s: "%(error)s"' % locals())
            else:
                self.logger.error(json.dumps(login_text))

    def login_challenge(self, checkpoint_url):
        self.session.headers.update({'Referer': BASE_URL})
        req = self.session.get(BASE_URL[:-1] + checkpoint_url)
        self.session.headers.update({'X-CSRFToken': req.cookies['csrftoken'], 'X-Instagram-AJAX': '1'})

        self.session.headers.update({'Referer': BASE_URL[:-1] + checkpoint_url})
        mode = input('Choose a challenge mode (0 - SMS, 1 - Email): ')
        challenge_data = {'choice': mode}
        challenge = self.session.post(BASE_URL[:-1] + checkpoint_url, data=challenge_data, allow_redirects=True)
        self.session.headers.update({'X-CSRFToken': challenge.cookies['csrftoken'], 'X-Instagram-AJAX': '1'})

        code = input('Enter code received: ')
        code_data = {'security_code': code}
        code = self.session.post(BASE_URL[:-1] + checkpoint_url, data=code_data, allow_redirects=True)
        self.session.headers.update({'X-CSRFToken': code.cookies['csrftoken']})
        self.cookies = code.cookies
        code_text = json.loads(code.text)

        if code_text.get('status') == 'ok':
            self.logged_in = True
        elif 'errors' in code.text:
            for count, error in enumerate(code_text['challenge']['errors']):
                count += 1
                self.logger.error('Session error %(count)s: "%(error)s"' % locals())
        else:
            self.logger.error(json.dumps(code_text))

    def logout(self):
        """Logs out of instagram."""
        if self.logged_in:
            try:
                logout_data = {'csrfmiddlewaretoken': self.cookies['csrftoken']}
                self.session.post(LOGOUT_URL, data=logout_data)
                self.logged_in = False
            except requests.exceptions.RequestException:
                self.logger.warning('Failed to log out ' + self.login_user)

    def make_dst_dir(self, username):
        """Creates the destination directory."""
        if self.destination == DESTINATION:
            dst = DESTINATION + username
        else:
            if self.retain_username:
                dst = self.destination + '/' + username
            else:
                dst = self.destination

        try:
            os.makedirs(dst)
        except OSError as err:
            if err.errno == errno.EEXIST and os.path.isdir(dst):
                # Directory already exists
                pass
            else:
                # Target dir exists as a file, or a different error
                raise

        # Resolve last scraped filetime
        if self.latest_stamps_parser:
            self.last_scraped_filemtime = self.get_last_scraped_timestamp(username)
        elif os.path.isdir(dst):
            self.last_scraped_filemtime = self.get_last_scraped_filemtime(dst)

        return dst

    def get_last_scraped_timestamp(self, username):
        if self.latest_stamps_parser:
            try:
                return self.latest_stamps_parser.getint(LATEST_STAMPS_USER_SECTION, username)
            except configparser.Error:
                pass
        return 0

    def set_last_scraped_timestamp(self, username, timestamp):
        if self.latest_stamps_parser:
            if not self.latest_stamps_parser.has_section(LATEST_STAMPS_USER_SECTION):
                self.latest_stamps_parser.add_section(LATEST_STAMPS_USER_SECTION)
            self.latest_stamps_parser.set(LATEST_STAMPS_USER_SECTION, username, str(timestamp))
            with open(self.latest_stamps, 'w') as f:
                self.latest_stamps_parser.write(f)

    def get_last_scraped_filemtime(self, dst):
        """Stores the last modified time of newest file in a directory."""
        list_of_files = []
        file_types = ('*.jpg', '*.mp4')

        for type in file_types:
            list_of_files.extend(glob.glob(dst + '/' + type))

        if list_of_files:
            latest_file = max(list_of_files, key=os.path.getmtime)
            return int(os.path.getmtime(latest_file))
        return 0

    def query_comments_gen(self, shortcode, end_cursor=''):
        """Generator for comments."""
        comments, end_cursor = self.__query_comments(shortcode, end_cursor)

        if comments:
            try:
                while True:
                    for item in comments:
                        yield item

                    if end_cursor:
                        comments, end_cursor = self.__query_comments(shortcode, end_cursor)
                    else:
                        return
            except ValueError:
                self.logger.exception('Failed to query comments for shortcode ' + shortcode)

    def __query_comments(self, shortcode, end_cursor=''):
        resp = self.get_json(QUERY_COMMENTS.format(shortcode, end_cursor))

        if resp is not None:
            payload = json.loads(resp)['data']['shortcode_media']

            if payload:
                container = payload['edge_media_to_comment']
                comments = [node['node'] for node in container['edges']]
                end_cursor = container['page_info']['end_cursor']
                return comments, end_cursor

        return None, None

    def scrape_hashtag(self):
        self.__scrape_query(self.query_hashtag_gen)

    def scrape_location(self):
        self.__scrape_query(self.query_location_gen)

    def worker_wrapper(self, fn, *args, **kwargs):
        try:
            if self.quit:
                return
            return fn(*args, **kwargs)
        except Exception as e:
            self.logger.debug("Exception in worker thread", exc_info=sys.exc_info())
            raise

    def __scrape_query(self, media_generator, executor=concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)):
        """Scrapes the specified value for posted media."""
        self.quit = False
        try:
            for value in self.usernames:
                self.posts = []
                self.last_scraped_filemtime = 0
                greatest_timestamp = 0
                future_to_item = {}

                dst = self.make_dst_dir(value)

                if self.include_location:
                    media_exec = concurrent.futures.ThreadPoolExecutor(max_workers=5)

                iter = 0
                for item in tqdm.tqdm(media_generator(value), desc='Searching {0} for posts'.format(value), unit=" media",
                                      disable=self.quiet):
                    if ((item['is_video'] is False and 'image' in self.media_types) or \
                                (item['is_video'] is True and 'video' in self.media_types)
                        ) and self.is_new_media(item):
                        future = executor.submit(self.worker_wrapper, self.download, item, dst)
                        future_to_item[future] = item

                    if self.include_location and 'location' not in item:
                        media_exec.submit(self.worker_wrapper, self.__get_location, item)

                    if self.comments:
                        item['edge_media_to_comment']['data'] = list(self.query_comments_gen(item['shortcode']))

                    if self.media_metadata or self.comments or self.include_location:
                        self.posts.append(item)

                    iter = iter + 1
                    if self.maximum != 0 and iter >= self.maximum:
                        break

                if future_to_item:
                    for future in tqdm.tqdm(concurrent.futures.as_completed(future_to_item),
                                            total=len(future_to_item),
                                            desc='Downloading', disable=self.quiet):
                        item = future_to_item[future]

                        if future.exception() is not None:
                            self.logger.warning(
                                'Media for {0} at {1} generated an exception: {2}'.format(value, item['urls'],
                                                                                          future.exception()))
                        else:
                            timestamp = self.__get_timestamp(item)
                            if timestamp > greatest_timestamp:
                                greatest_timestamp = timestamp
                # Even bother saving it?
                if greatest_timestamp > self.last_scraped_filemtime:
                    self.set_last_scraped_timestamp(value, greatest_timestamp)

                if (self.media_metadata or self.comments or self.include_location) and self.posts:
                    self.save_json(self.posts, '{0}/{1}.json'.format(dst, value))
        finally:
            self.quit = True

    def query_hashtag_gen(self, hashtag):
        return self.__query_gen(QUERY_HASHTAG, 'hashtag', hashtag)

    def query_location_gen(self, location):
        return self.__query_gen(QUERY_LOCATION, 'location', location)


    def __query_gen(self, url, entity_name, query, end_cursor=''):
        """Generator for hashtag and location."""
        nodes, end_cursor = self.__query(url, entity_name, query, end_cursor)

        if nodes:
            try:
                while True:
                    for node in nodes:
                        yield node

                    if end_cursor:
                        nodes, end_cursor = self.__query(url, entity_name, query, end_cursor)
                    else:
                        return
            except ValueError:
                self.logger.exception('Failed to query ' + query)

    def __query(self, url, entity_name, query, end_cursor):
        resp = self.get_json(url.format(query, end_cursor))

        if resp is not None:
            payload = json.loads(resp)['data'][entity_name]

            if payload:
                nodes = []

                if end_cursor == '':
                    top_posts = payload['edge_' + entity_name + '_to_top_posts']
                    nodes.extend(self._get_nodes(top_posts))

                posts = payload['edge_' + entity_name + '_to_media']

                nodes.extend(self._get_nodes(posts))
                end_cursor = posts['page_info']['end_cursor']
                return nodes, end_cursor

        return None, None

    def _get_nodes(self, container):
        return [self.augment_node(node['node']) for node in container['edges']]

    def augment_node(self, node):
        self.extract_tags(node)

        details = None
        if self.include_location and 'location' not in node:
            details = self.__get_media_details(node['shortcode'])
            node['location'] = details.get('location') if details else None

        if 'urls' not in node:
            node['urls'] = []

        if node['is_video'] and 'video_url' in node:
            node['urls'] = [node['video_url']]
        elif '__typename' in node and node['__typename'] == 'GraphImage':
            node['urls'] = [self.get_original_image(node['display_url'])]
        else:
            if details is None:
                details = self.__get_media_details(node['shortcode'])

            if details:
                if '__typename' in details and details['__typename'] == 'GraphVideo':
                    node['urls'] = [details['video_url']]
                elif '__typename' in details and details['__typename'] == 'GraphSidecar':
                    urls = []
                    for carousel_item in details['edge_sidecar_to_children']['edges']:
                        urls += self.augment_node(carousel_item['node'])['urls']
                    node['urls'] = urls
                else:
                    node['urls'] = [self.get_original_image(details['display_url'])]

        return node

    def __get_media_details(self, shortcode):
        resp = self.get_json(VIEW_MEDIA_URL.format(shortcode))

        if resp is not None:
            try:
                return json.loads(resp)['graphql']['shortcode_media']
            except ValueError:
                self.logger.warning('Failed to get media details for ' + shortcode)

        else:
            self.logger.warning('Failed to get media details for ' + shortcode)

    def __get_location(self, item):
        code = item.get('shortcode', item.get('code'))

        if code:
            details = self.__get_media_details(code)
            item['location'] = details.get('location')


    def scrape(self, executor=concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)):
        """Crawls through and downloads user's media"""
        if self.login_user and self.login_pass:
            self.login()
            if not self.logged_in and self.login_only:
                self.logger.warning('Fallback anonymous scraping disabled')
                return

        self.quit = False
        try:
            for username in self.usernames:
                self.posts = []
                self.last_scraped_filemtime = 0
                greatest_timestamp = 0
                future_to_item = {}

                dst = self.make_dst_dir(username)

                # Get the user metadata.
                user = self.fetch_user(username)
                if user:
                    self.get_profile_pic(dst, executor, future_to_item, user, username)
                    self.get_stories(dst, executor, future_to_item, user, username)

                    if self.followers:
                        self.get_followers(dst, executor, future_to_item, user, username)

                    if self.following:
                        self.get_following(dst, executor, future_to_item, user, username)

                    if self.unfollow:
                        self.perform_bulk_unfollow()

                    if self.bulk_follow:
                        self.perform_bulk_follow()



                # Crawls the media and sends it to the executor.
                try:
                    # if we don't care about the media
                    if self.no_media:
                        # skip
                        continue

                    user = self.get_user(username)


                    if not user:
                        continue

                    self.save_json(user, '{0}/user.json'.format(dst))

                    self.get_media(dst, executor, future_to_item, user)

                    # Displays the progress bar of completed downloads. Might not even pop up if all media is downloaded while
                    # the above loop finishes.
                    if future_to_item:
                        for future in tqdm.tqdm(concurrent.futures.as_completed(future_to_item), total=len(future_to_item),
                                                desc='Downloading', disable=self.quiet):
                            item = future_to_item[future]

                            if future.exception() is not None:
                                self.logger.error(
                                    'Media at {0} generated an exception: {1}'.format(item['urls'], future.exception()))
                            else:
                                timestamp = self.__get_timestamp(item)
                                if timestamp > greatest_timestamp:
                                    greatest_timestamp = timestamp
                    # Even bother saving it?
                    if greatest_timestamp > self.last_scraped_filemtime:
                        self.set_last_scraped_timestamp(username, greatest_timestamp)

                    if (self.media_metadata or self.comments or self.include_location) and self.posts:
                        self.save_json(self.posts, '{0}/{1}.json'.format(dst, username))
                except ValueError:
                    self.logger.error("Unable to scrape user - %s" % username)
        finally:
            self.quit = True
            self.logout()

    def get_profile_pic(self, dst, executor, future_to_item, user, username):
        # Download the profile pic if not the default.
        if 'image' in self.media_types and 'profile_pic_url_hd' in user \
                and '11906329_960233084022564_1448528159' not in user['profile_pic_url_hd']:
            item = {'urls': [self.get_original_image(user['profile_pic_url_hd'])],
                    'created_time': 1286323200}

            if self.latest is False or os.path.isfile(dst + '/' + item['urls'][0].split('/')[-1]) is False:
                for item in tqdm.tqdm([item], desc='Searching {0} for profile pic'.format(username), unit=" images",
                                      ncols=0, disable=self.quiet):
                    future = executor.submit(self.worker_wrapper, self.download, item, dst)
                    future_to_item[future] = item

    def get_followers(self, dst, executor, future_to_item, user, username):
        if self.logged_in and self.followers:
            self._followers = []
            for item in tqdm.tqdm(self.query_followers_gen(user), desc="Searching {0} for followers".format(username), disable=self.quiet, unit=" follower"):
                self._followers.append(item)
                self.save_json(self._followers, "./data/{0}/followers.json".format(username))
            # self.save_json(self._followers, "./data/{0}/followers.json".format(username))

    def get_following(self, dst, executor, future_to_item, user, username):
        if self.logged_in and self.following:
            self._following = []
            for item in tqdm.tqdm(self.query_following_gen(user), desc="Searching {0} for following".format(username), disable=self.quiet, unit=" following"):
                self._following.append(item)
            self.save_json(self._following, "./data/{0}/following.json".format(username))



    def get_stories(self, dst, executor, future_to_item, user, username):
        """Scrapes the user's stories."""
        if self.logged_in and \
                ('story-image' in self.media_types or 'story-video' in self.media_types):
            # Get the user's stories.
            stories = self.fetch_stories(user['id'])

            # Downloads the user's stories and sends it to the executor.
            iter = 0
            for item in tqdm.tqdm(stories, desc='Searching {0} for stories'.format(username), unit=" media",
                                  disable=self.quiet):
                if self.story_has_selected_media_types(item) and self.is_new_media(item):
                    future = executor.submit(self.worker_wrapper, self.download, item, dst)
                    future_to_item[future] = item

                iter = iter + 1
                if self.maximum != 0 and iter >= self.maximum:
                    break

    def get_user(self, username):
        """Fetches the user's metadata."""
        url = USER_URL.format(username)

        resp = self.get_json(url)

        if resp is not None:
            user = json.loads(resp)['user']
            if user and user['is_private'] and user['media']['count'] > 0 and not user['media']['nodes']:
                self.logger.error('User {0} is private'.format(username))
            return user
        else:
            self.logger.error('Error getting user details for {0}. Please verify that the user exists.'.format(username))

    def get_media(self, dst, executor, future_to_item, user):
        """Scrapes the user's posts for media."""
        if 'image' not in self.media_types and 'video' not in self.media_types:
            return

        username = user['username']

        if self.include_location:
            media_exec = concurrent.futures.ThreadPoolExecutor(max_workers=5)

        iter = 0
        for item in tqdm.tqdm(self.query_media_gen(user), desc='Searching {0} for posts'.format(username),
                              unit=' media', disable=self.quiet):
            # -Filter command line
            if self.filter:
                if 'tags' in item:
                    filtered = any(x in item['tags'] for x in self.filter)
                    if self.has_selected_media_types(item) and self.is_new_media(item) and filtered:
                        future = executor.submit(self.worker_wrapper, self.download, item, dst)
                        future_to_item[future] = item
                else:
                    # For when filter is on but media doesnt contain tags
                    pass
            # --------------#
            else:
                if self.has_selected_media_types(item) and self.is_new_media(item):
                    future = executor.submit(self.worker_wrapper, self.download, item, dst)
                    future_to_item[future] = item

            if self.include_location:
                media_exec.submit(self.worker_wrapper, self.__get_location, item)

            if self.comments:
                item['comments'] = {'data': list(self.query_comments_gen(item['shortcode']))}

            if self.media_metadata or self.comments or self.include_location:
                self.posts.append(item)

            iter = iter + 1
            if self.maximum != 0 and iter >= self.maximum:
                break

    def fetch_user(self, username):
        """Fetches the user's metadata."""
        resp = self.get_json(BASE_URL + username)

        if resp is not None and '_sharedData' in resp:
            try:
                shared_data = resp.split("window._sharedData = ")[1].split(";</script>")[0]
                return json.loads(shared_data)['entry_data']['ProfilePage'][0]['user']
            except (TypeError, KeyError, IndexError):
                pass

    def fetch_stories(self, user_id):
        """Fetches the user's stories."""
        resp = self.get_json(STORIES_URL.format(user_id), headers={
            'user-agent': STORIES_UA,
            'cookie': STORIES_COOKIE.format(self.cookies['ds_user_id'], self.cookies['sessionid'])
        })

        if resp is not None:
            retval = json.loads(resp)
            if retval['reel'] and 'items' in retval['reel'] and len(retval['reel']['items']) > 0:
                return [self.set_story_url(item) for item in retval['reel']['items']]

        return []

    def query_media_gen(self, user, end_cursor=''):
        """Generator for media."""
        media, end_cursor = self.__query_media(user['id'], end_cursor)

        if media:
            try:
                while True:
                    for item in media:
                        if not self.is_new_media(item):
                            return
                        yield item

                    if end_cursor:
                        media, end_cursor = self.__query_media(user['id'], end_cursor)
                    else:
                        return
            except ValueError:
                self.logger.exception('Failed to query media for user ' + user['username'])

    def __query_media(self, id, end_cursor=''):
        resp = self.get_json(QUERY_MEDIA.format(id, end_cursor))

        if resp is not None:
            payload = json.loads(resp)['data']['user']

            if payload:
                container = payload['edge_owner_to_timeline_media']
                nodes = self._get_nodes(container)
                end_cursor = container['page_info']['end_cursor']
                return nodes, end_cursor

        return None, None

    def is_new_follower(self, follower):
        return True


    def query_followers_gen(self, user, end_cursor=''):
        followers, end_cursor = self.__query_followers(user['id'], end_cursor)

        if followers:
            try:
                while True:
                    for follower in followers:
                        if not self.is_new_follower(follower):
                            return
                        yield follower
                    if end_cursor:
                        followers, end_cursor = self.__query_followers(user['id'], end_cursor)
                    else:
                        return
            except ValueError:
                self.logger.exception("Failed to query followers for user " + user['username'])

    def __query_followers(self, id, end_cursor=''):
        resp = self.get_json(QUERY_FOLLOWERS.format(id, end_cursor))

        if resp is not None:
            payload = json.loads(resp)['data']['user']

            if payload:
                container = payload['edge_followed_by']
                # ignore the _get_nodes function which is mostly for images
                nodes = [node['node'] for node in container['edges']]
                end_cursor = container['page_info']['end_cursor']
                return nodes, end_cursor
        return None, None

    def is_new_following(self, following):
        return True

    def query_following_gen(self, user, end_cursor=''):
        following, end_cursor = self.__query_following(user['id'], end_cursor)

        if following:
            try:
                while True:
                    for follower in following:
                        if not self.is_new_following(follower):
                            return
                        yield follower
                    if end_cursor:
                        following, end_cursor = self.__query_following(user['id'], end_cursor)
                    else:
                        return
            except ValueError:
                self.logger.exception("Failed to query following for user " + user['username'])

    def __query_following(self, id, end_cursor):
        resp = self.get_json(QUERY_FOLLOWING.format(id, end_cursor))

        if resp is not None:
            payload = json.loads(resp)['data']['user']

            if payload:
                container = payload['edge_follow']
                # ignore the _get_nodes function which is mostly for images
                nodes = [node['node'] for node in container['edges']]
                end_cursor = container['page_info']['end_cursor']
                return nodes, end_cursor
        return None, None

    def has_selected_media_types(self, item):
        filetypes = {'jpg': 0, 'mp4': 0}

        for url in item['urls']:
            ext = self.__get_file_ext(url)
            if ext not in filetypes:
                filetypes[ext] = 0
            filetypes[ext] += 1

        if ('image' in self.media_types and filetypes['jpg'] > 0) or \
                ('video' in self.media_types and filetypes['mp4'] > 0):
            return True

        return False

    def story_has_selected_media_types(self, item):
        # media_type 1 is image, 2 is video
        if item['media_type'] == 1 and 'story-image' in self.media_types:
            return True
        if item['media_type'] == 2 and 'story-video' in self.media_types:
            return True

        return False

    def extract_tags(self, item):
        """Extracts the hashtags from the caption text."""
        caption_text = ''
        if 'caption' in item and item['caption']:
            if isinstance(item['caption'], dict):
                caption_text = item['caption']['text']
            else:
                caption_text = item['caption']

        elif 'edge_media_to_caption' in item and item['edge_media_to_caption'] and item['edge_media_to_caption'][
            'edges']:
            caption_text = item['edge_media_to_caption']['edges'][0]['node']['text']

        if caption_text:
            # include words and emojis
            item['tags'] = re.findall(
                r"(?<!&)#(\w+|(?:[\xA9\xAE\u203C\u2049\u2122\u2139\u2194-\u2199\u21A9\u21AA\u231A\u231B\u2328\u2388\u23CF\u23E9-\u23F3\u23F8-\u23FA\u24C2\u25AA\u25AB\u25B6\u25C0\u25FB-\u25FE\u2600-\u2604\u260E\u2611\u2614\u2615\u2618\u261D\u2620\u2622\u2623\u2626\u262A\u262E\u262F\u2638-\u263A\u2648-\u2653\u2660\u2663\u2665\u2666\u2668\u267B\u267F\u2692-\u2694\u2696\u2697\u2699\u269B\u269C\u26A0\u26A1\u26AA\u26AB\u26B0\u26B1\u26BD\u26BE\u26C4\u26C5\u26C8\u26CE\u26CF\u26D1\u26D3\u26D4\u26E9\u26EA\u26F0-\u26F5\u26F7-\u26FA\u26FD\u2702\u2705\u2708-\u270D\u270F\u2712\u2714\u2716\u271D\u2721\u2728\u2733\u2734\u2744\u2747\u274C\u274E\u2753-\u2755\u2757\u2763\u2764\u2795-\u2797\u27A1\u27B0\u27BF\u2934\u2935\u2B05-\u2B07\u2B1B\u2B1C\u2B50\u2B55\u3030\u303D\u3297\u3299]|\uD83C[\uDC04\uDCCF\uDD70\uDD71\uDD7E\uDD7F\uDD8E\uDD91-\uDD9A\uDE01\uDE02\uDE1A\uDE2F\uDE32-\uDE3A\uDE50\uDE51\uDF00-\uDF21\uDF24-\uDF93\uDF96\uDF97\uDF99-\uDF9B\uDF9E-\uDFF0\uDFF3-\uDFF5\uDFF7-\uDFFF]|\uD83D[\uDC00-\uDCFD\uDCFF-\uDD3D\uDD49-\uDD4E\uDD50-\uDD67\uDD6F\uDD70\uDD73-\uDD79\uDD87\uDD8A-\uDD8D\uDD90\uDD95\uDD96\uDDA5\uDDA8\uDDB1\uDDB2\uDDBC\uDDC2-\uDDC4\uDDD1-\uDDD3\uDDDC-\uDDDE\uDDE1\uDDE3\uDDEF\uDDF3\uDDFA-\uDE4F\uDE80-\uDEC5\uDECB-\uDED0\uDEE0-\uDEE5\uDEE9\uDEEB\uDEEC\uDEF0\uDEF3]|\uD83E[\uDD10-\uDD18\uDD80-\uDD84\uDDC0]|(?:0\u20E3|1\u20E3|2\u20E3|3\u20E3|4\u20E3|5\u20E3|6\u20E3|7\u20E3|8\u20E3|9\u20E3|#\u20E3|\\*\u20E3|\uD83C(?:\uDDE6\uD83C(?:\uDDEB|\uDDFD|\uDDF1|\uDDF8|\uDDE9|\uDDF4|\uDDEE|\uDDF6|\uDDEC|\uDDF7|\uDDF2|\uDDFC|\uDDE8|\uDDFA|\uDDF9|\uDDFF|\uDDEA)|\uDDE7\uD83C(?:\uDDF8|\uDDED|\uDDE9|\uDDE7|\uDDFE|\uDDEA|\uDDFF|\uDDEF|\uDDF2|\uDDF9|\uDDF4|\uDDE6|\uDDFC|\uDDFB|\uDDF7|\uDDF3|\uDDEC|\uDDEB|\uDDEE|\uDDF6|\uDDF1)|\uDDE8\uD83C(?:\uDDF2|\uDDE6|\uDDFB|\uDDEB|\uDDF1|\uDDF3|\uDDFD|\uDDF5|\uDDE8|\uDDF4|\uDDEC|\uDDE9|\uDDF0|\uDDF7|\uDDEE|\uDDFA|\uDDFC|\uDDFE|\uDDFF|\uDDED)|\uDDE9\uD83C(?:\uDDFF|\uDDF0|\uDDEC|\uDDEF|\uDDF2|\uDDF4|\uDDEA)|\uDDEA\uD83C(?:\uDDE6|\uDDE8|\uDDEC|\uDDF7|\uDDEA|\uDDF9|\uDDFA|\uDDF8|\uDDED)|\uDDEB\uD83C(?:\uDDF0|\uDDF4|\uDDEF|\uDDEE|\uDDF7|\uDDF2)|\uDDEC\uD83C(?:\uDDF6|\uDDEB|\uDDE6|\uDDF2|\uDDEA|\uDDED|\uDDEE|\uDDF7|\uDDF1|\uDDE9|\uDDF5|\uDDFA|\uDDF9|\uDDEC|\uDDF3|\uDDFC|\uDDFE|\uDDF8|\uDDE7)|\uDDED\uD83C(?:\uDDF7|\uDDF9|\uDDF2|\uDDF3|\uDDF0|\uDDFA)|\uDDEE\uD83C(?:\uDDF4|\uDDE8|\uDDF8|\uDDF3|\uDDE9|\uDDF7|\uDDF6|\uDDEA|\uDDF2|\uDDF1|\uDDF9)|\uDDEF\uD83C(?:\uDDF2|\uDDF5|\uDDEA|\uDDF4)|\uDDF0\uD83C(?:\uDDED|\uDDFE|\uDDF2|\uDDFF|\uDDEA|\uDDEE|\uDDFC|\uDDEC|\uDDF5|\uDDF7|\uDDF3)|\uDDF1\uD83C(?:\uDDE6|\uDDFB|\uDDE7|\uDDF8|\uDDF7|\uDDFE|\uDDEE|\uDDF9|\uDDFA|\uDDF0|\uDDE8)|\uDDF2\uD83C(?:\uDDF4|\uDDF0|\uDDEC|\uDDFC|\uDDFE|\uDDFB|\uDDF1|\uDDF9|\uDDED|\uDDF6|\uDDF7|\uDDFA|\uDDFD|\uDDE9|\uDDE8|\uDDF3|\uDDEA|\uDDF8|\uDDE6|\uDDFF|\uDDF2|\uDDF5|\uDDEB)|\uDDF3\uD83C(?:\uDDE6|\uDDF7|\uDDF5|\uDDF1|\uDDE8|\uDDFF|\uDDEE|\uDDEA|\uDDEC|\uDDFA|\uDDEB|\uDDF4)|\uDDF4\uD83C\uDDF2|\uDDF5\uD83C(?:\uDDEB|\uDDF0|\uDDFC|\uDDF8|\uDDE6|\uDDEC|\uDDFE|\uDDEA|\uDDED|\uDDF3|\uDDF1|\uDDF9|\uDDF7|\uDDF2)|\uDDF6\uD83C\uDDE6|\uDDF7\uD83C(?:\uDDEA|\uDDF4|\uDDFA|\uDDFC|\uDDF8)|\uDDF8\uD83C(?:\uDDFB|\uDDF2|\uDDF9|\uDDE6|\uDDF3|\uDDE8|\uDDF1|\uDDEC|\uDDFD|\uDDF0|\uDDEE|\uDDE7|\uDDF4|\uDDF8|\uDDED|\uDDE9|\uDDF7|\uDDEF|\uDDFF|\uDDEA|\uDDFE)|\uDDF9\uD83C(?:\uDDE9|\uDDEB|\uDDFC|\uDDEF|\uDDFF|\uDDED|\uDDF1|\uDDEC|\uDDF0|\uDDF4|\uDDF9|\uDDE6|\uDDF3|\uDDF7|\uDDF2|\uDDE8|\uDDFB)|\uDDFA\uD83C(?:\uDDEC|\uDDE6|\uDDF8|\uDDFE|\uDDF2|\uDDFF)|\uDDFB\uD83C(?:\uDDEC|\uDDE8|\uDDEE|\uDDFA|\uDDE6|\uDDEA|\uDDF3)|\uDDFC\uD83C(?:\uDDF8|\uDDEB)|\uDDFD\uD83C\uDDF0|\uDDFE\uD83C(?:\uDDF9|\uDDEA)|\uDDFF\uD83C(?:\uDDE6|\uDDF2|\uDDFC))))[\ufe00-\ufe0f\u200d]?)+",
                caption_text, re.UNICODE)
            item['tags'] = list(set(item['tags']))

        return item

    def get_original_image(self, url):
        """Gets the full-size image from the specified url."""
        # these path parts somehow prevent us from changing the rest of media url
        url = re.sub(r'/vp/[0-9A-Fa-f]{32}/[0-9A-Fa-f]{8}/', '/', url)
        # remove dimensions to get largest image
        url = re.sub(r'/[sp]\d{3,}x\d{3,}/', '/', url)
        # get non-square image if one exists
        url = re.sub(r'/c\d{1,}.\d{1,}.\d{1,}.\d{1,}/', '/', url)
        return url

    def set_story_url(self, item):
        """Sets the story url."""
        urls = []
        if 'video_versions' in item:
            urls.append(item['video_versions'][0]['url'])
        if 'image_versions2' in item:
            urls.append(item['image_versions2']['candidates'][0]['url'].split('?')[0])
        item['urls'] = urls
        return item

    def download(self, item, save_dir=DESTINATION):
        """Downloads the media file."""
        for url in item['urls']:
            base_name = url.split('/')[-1].split('?')[0]
            file_path = os.path.join(save_dir, base_name)

            if not os.path.isfile(file_path):
                headers = {'Host': urlparse(url).hostname}

                part_file = file_path + '.part'
                downloaded = 0
                total_length = None
                with open(part_file, 'wb') as media_file:
                    try:
                        retry = 0
                        retry_delay = RETRY_DELAY
                        while(True):
                            if self.quit:
                                return
                            try:
                                downloaded_before = downloaded
                                if downloaded_before != 0:
                                    headers['Range'] = 'bytes={0}-{1}'.format(downloaded_before, total_length-1)

                                with self.session.get(url, headers=headers, stream=True, timeout=CONNECT_TIMEOUT) as response:
                                    if response.status_code == 404:
                                        #instagram don't lie on this
                                        break
                                    response.raise_for_status()

                                    if downloaded_before == 0:
                                        content_length = response.headers.get('Content-Length')
                                        if content_length is None:
                                            raise PartialContentException('Partial response')
                                        total_length = int(content_length)
                                        media_file.truncate(total_length)

                                    for chunk in response.iter_content(chunk_size=64*1024):
                                        if chunk:
                                            downloaded += len(chunk)
                                            media_file.write(chunk)
                                        if self.quit:
                                            return

                                if downloaded != total_length:
                                    raise PartialContentException('Got first {0} bytes from {1}'.format(downloaded, total_length))

                                break

                            # In case of exception part_file is not removed on purpose,
                            # it is easier to exemine it later when analising logs.
                            # Please do not add os.remove here.
                            except (KeyboardInterrupt):
                                raise
                            except (requests.exceptions.RequestException, PartialContentException) as e:
                                if downloaded - downloaded_before > 0:
                                    # if we got some data on this iteration do not count it as a failure
                                    self.logger.warning('Continue after exception {0} on {1}'.format(repr(e), url))
                                    retry = 0 # the next fail will be first in a row with no data
                                    continue
                                if retry < MAX_RETRIES:
                                    self.logger.warning('Retry after exception {0} on {1}'.format(repr(e), url))
                                    self.sleep(retry_delay)
                                    retry_delay = min( 2 * retry_delay, MAX_RETRY_DELAY )
                                    retry = retry + 1
                                    continue
                                else:
                                    keep_trying = self._retry_prompt(url, repr(e))
                                    if keep_trying == True:
                                        retry = 0
                                        continue
                                    elif keep_trying == False:
                                        break
                                raise
                    finally:
                        media_file.truncate(downloaded)

                if downloaded == total_length:
                    os.rename(part_file, file_path)
                    timestamp = self.__get_timestamp(item)
                    file_time = int(timestamp if timestamp else time.time())
                    os.utime(file_path, (file_time, file_time))

    def is_new_media(self, item):
        """Returns True if the media is new."""
        if self.latest is False or self.last_scraped_filemtime == 0:
            return True

        current_timestamp = self.__get_timestamp(item)
        return current_timestamp > 0 and current_timestamp >= self.last_scraped_filemtime

    @staticmethod
    def __get_timestamp(item):
        if item:
            for key in ['taken_at_timestamp', 'created_time', 'taken_at', 'date']:
                found = item.get(key, 0)
                try:
                    found = int(found)
                    if found > 1:  # >1 to ignore any boolean casts
                        return found
                except ValueError:
                    pass
        return 0

    @staticmethod
    def __get_file_ext(path):
        return os.path.splitext(path)[1][1:].strip().lower()

    @staticmethod
    def __search(query):
        resp = requests.get(SEARCH_URL.format(query))
        return json.loads(resp.text)

    def search_locations(self):
        query = ' '.join(self.usernames)
        result = self.__search(query)

        if len(result['places']) == 0:
            raise ValueError("No locations found for query '{0}'".format(query))

        sorted_places = sorted(result['places'], key=itemgetter('position'))

        for item in sorted_places[0:5]:
            place = item['place']
            print('location-id: {0}, title: {1}, subtitle: {2}, city: {3}, lat: {4}, lng: {5}'.format(
                place['location']['pk'],
                place['title'],
                place['subtitle'],
                place['location']['city'],
                place['location']['lat'],
                place['location']['lng']
            ))

    @staticmethod
    def save_json(data, dst=DESTINATION):
        """Saves the data to a json file."""
        if data:
            with open(dst, 'wb') as f:
                json.dump(data, codecs.getwriter('utf-8')(f), indent=4, sort_keys=True, ensure_ascii=False)

    @staticmethod
    def get_logger(level=logging.DEBUG, verbose=0):
        """Returns a logger."""
        logger = logging.getLogger(__name__)

        fh = logging.FileHandler('instagram-scraper.log', 'w')
        fh.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        fh.setLevel(level)
        logger.addHandler(fh)

        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
        sh_lvls = [logging.ERROR, logging.WARNING, logging.INFO]
        sh.setLevel(sh_lvls[verbose])
        logger.addHandler(sh)

        logger.setLevel(level)

        return logger

    @staticmethod
    def parse_file_usernames(usernames_file):
        """Parses a file containing a list of usernames."""
        users = []

        try:
            with open(usernames_file) as user_file:
                for line in user_file.readlines():
                    # Find all usernames delimited by ,; or whitespace
                    users += re.findall(r'[^,;\s]+', line.split("#")[0])
        except IOError as err:
            raise ValueError('File not found ' + err)

        return users

    @staticmethod
    def parse_delimited_str(input):
        """Parse the string input as a list of delimited tokens."""
        return re.findall(r'[^,;\s]+', input)


class Command(BaseCommand):
    help = "scrapes instagram"

    def add_arguments(self, parser):
        parser.fromfile_prefix_chars = '@'

        parser.add_argument('username', help='Instagram user(s) to scrape', nargs='*')
        parser.add_argument('--destination', '-d', default=DESTINATION, help='Download destination')
        parser.add_argument('--login-user', '--login_user', '-u', default=None, help='Instagram login user')
        parser.add_argument('--login-pass', '--login_pass', '-p', default=None, help='Instagram login password')
        parser.add_argument('--followers', action='store_true', default=False, help='Scrape the followers of the provided username')
        parser.add_argument('--following', action='store_true', default=False, help='Scrape the users the provided username follows')
        parser.add_argument('--unfollow', action='store_true', default=False, help='Unfollow the users who dont follow you back')
        parser.add_argument('--bulk_follow', default=None, help="A file containing users to follow")
        parser.add_argument('--no_media', '--no-media', action="store_true", default=False, help="Whether to scrape the media")
        parser.add_argument('--login-only', '--login_only', '-l', default=False, action='store_true',
                            help='Disable anonymous fallback if login fails')
        parser.add_argument('--filename', '-f', help='Path to a file containing a list of users to scrape')
        parser.add_argument('--quiet', '-q', default=False, action='store_true', help='Be quiet while scraping')
        parser.add_argument('--maximum', '-m', type=int, default=0, help='Maximum number of items to scrape')
        parser.add_argument('--retain-username', '--retain_username', '-n', action='store_true', default=False,
                            help='Creates username subdirectory when destination flag is set')
        parser.add_argument('--media-metadata', '--media_metadata', action='store_true', default=False,
                            help='Save media metadata to json file')
        parser.add_argument('--include-location', '--include_location', action='store_true', default=False,
                            help='Include location data when saving media metadata')
        parser.add_argument('--media-types', '--media_types', '-t', nargs='+', default=['image', 'video', 'story'],
                            help='Specify media types to scrape')
        parser.add_argument('--latest', action='store_true', default=False, help='Scrape new media since the last scrape')
        parser.add_argument('--latest-stamps', '--latest_stamps', default=None,
                            help='Scrape new media since timestamps by user in specified file')
        parser.add_argument('--tag', action='store_true', default=False, help='Scrape media using a hashtag')
        parser.add_argument('--filter', default=None, help='Filter by tags in user posts', nargs='*')
        parser.add_argument('--location', action='store_true', default=False, help='Scrape media using a location-id')
        parser.add_argument('--search-location', action='store_true', default=False, help='Search for locations by name')
        parser.add_argument('--comments', action='store_true', default=False, help='Save post comments to json file')
        parser.add_argument('--interactive', '-i', action='store_true', default=False,
                            help='Enable interactive login challenge solving')
        parser.add_argument('--retry-forever', action='store_true', default=False,
                            help='Retry download attempts endlessly when errors are received')
        # parser.add_argument('--verbose', '-v', type=int, default=0, help='Logging verbosity level')

    def handle(self, *args, **options):
        # python manage.py scrape_instagram @app/management/commands/instagram/config/args.txt

        print "These are the args", options

        args = Struct(**options)
        print args

        if (args.login_user and args.login_pass is None) or (args.login_user is None and args.login_pass):
        #     parser.print_help()
            raise ValueError('Must provide login user AND password')

        if args.bulk_follow is not None:
            if len(args.username) != 1 or args.login_user not in args.username:
                raise ValueError("Must login as self to follow")
            args.following = True

        if args.unfollow is True:
            if len(args.username) != 1 or args.login_user not in args.username:
                raise ValueError("Must login as self to ufollow")
            args.followers = True
            args.following = True

        if (args.followers is True or args.following is True) and (args.login_user is None or args.login_pass is None):
        #     parser.print_help()
            raise ValueError('Must login to scrape following and followers')

        if args.followers or args.following:
            args.login_only = True

        if not args.username and args.filename is None:
        #     parser.print_help()
            raise ValueError('Must provide username(s) OR a file containing a list of username(s)')

        elif args.username and args.filename:
        #     parser.print_help()
            raise ValueError('Must provide only one of the following: username(s) OR a filename containing username(s)')

        if args.tag and args.location:
        #     parser.print_help()
            raise ValueError('Must provide only one of the following: hashtag OR location')

        if args.tag and args.filter:
            # parser.print_help()
            raise ValueError('Filters apply to user posts')


        if args.filename:
            args.usernames = InstagramScraper.parse_file_usernames(args.filename)
        else:
            args.usernames = InstagramScraper.parse_delimited_str(','.join(args.username))

        if args.media_types and len(args.media_types) == 1 and re.compile(r'[,;\s]+').findall(args.media_types[0]):
            args.media_types = InstagramScraper.parse_delimited_str(args.media_types[0])

        if args.retry_forever:
            global MAX_RETRIES
            MAX_RETRIES = sys.maxsize


        scraper = InstagramScraper(**vars(args))

        if args.tag:
            scraper.scrape_hashtag()
        elif args.location:
            scraper.scrape_location()
        elif args.search_location:
            scraper.search_locations()
        else:
            scraper.scrape()


