from __future__ import unicode_literals

from django.db import models


class TimeStamp(models.Model):
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

class Person(TimeStamp):
    class Meta:
        abstract = True

class InstagramUser(TimeStamp):
    # the unique identifier in instagram's system (given as a string
    # that represents an integer)
    instagram_id = models.CharField(max_length=255)
    full_name = models.CharField(max_length=255)
    profile_pic_url = models.CharField(max_length=255)
    username = models.CharField(max_length=255)

    # ^ these are the four parameters provided when querying followers

    followed_by_count = models.IntegerField()
    follows_count = models.IntegerField()
    biography = models.TextField()
    is_private = models.BooleanField(default=False)

    profile_pic_url_hd = models.CharField(max_length=255, blank=True, null=True)

    # a cache of the number of images they've posted,
    # so that we can easily check if we should scrape again
    media_count = models.IntegerField(blank=True, null=True)

    # getting the user also send you the first page of their media
    # files so you could also cache that json but that might be a waste of
    # space

    # we're most often going to be studying the followers of a particular
    # user so I make the design decision to key in this way. Could alternatively
    # have created a "FollowEdge" model, and that may make the most sense if we
    # want to timestamp things so we can say how people follow you over time and stuff
    # but idk if thats useful
    followers = models.ManyToManyField("self", related_name="follows")

class InstagramCluster(models.Model):
    # groupings of instagram users, eg. all of those that follow Burberry
    # (this doesn't make a ton of sense because it is modeled by the location data)
    name = models.CharField(max_length=255)
    description = models.TextField()
    instagram_users = models.ManyToManyField(InstagramUser, related_name="clusters")

class InstagramBot(models.Model):
    username = models.CharField(max_length=255)
    # the plaintext password for this account. (we don't really care
    # that much if they're hacked because its just a bog)
    password = models.CharField(max_length=255)


class Scrape(TimeStamp):
    '''
    Instances of this class represent one run through
    scraping, allowing us to figure out which
    '''
    # the user being scraped
    instagram_user = models.ForeignKey(InstagramUser, blank=True, null=True)

    # the bot account we were doing the scraping as
    instagram_bot = models.ForeignKey(InstagramBot, blank=True, null=True)

    # some indication of where the scrape stopped

    class Meta:
        abstract = True


class Organization(models.Model):
    '''
    e.g. Burberry
    '''

    # the instagram user this organization is associated with
    instagram_user = models.ForeignKey(InstagramUser, blank=True, null=True)

class InstagramPost(TimeStamp):
    # the json dump that represents the post object (I just don't want to type all the
    # fields out right now)
    post_json = models.TextField()
    instagram_user = models.ForeignKey(InstagramUser, related_name="posts")

class FashionBlogger(TimeStamp):
    name = models.CharField(max_length=255)
    blog_url = models.CharField(max_length=255, blank=True, null=True)
    instagram_user = models.ForeignKey(InstagramUser, blank=True, null=True)


