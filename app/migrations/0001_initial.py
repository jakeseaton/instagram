# -*- coding: utf-8 -*-
# Generated by Django 1.9.1 on 2018-02-28 03:41
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='InstagramBot',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('username', models.CharField(max_length=255)),
                ('password', models.CharField(max_length=255)),
            ],
        ),
        migrations.CreateModel(
            name='InstagramPost',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('post_json', models.TextField()),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='InstagramUser',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('instagram_id', models.CharField(max_length=255)),
                ('full_name', models.CharField(max_length=255)),
                ('profile_pic_url', models.CharField(max_length=255)),
                ('username', models.CharField(max_length=255)),
                ('followed_by_count', models.IntegerField()),
                ('follows_count', models.IntegerField()),
                ('biography', models.TextField()),
                ('is_private', models.BooleanField(default=False)),
                ('profile_pic_url_hd', models.CharField(blank=True, max_length=255, null=True)),
                ('media_count', models.IntegerField(blank=True, null=True)),
                ('followers', models.ManyToManyField(related_name='_instagramuser_followers_+', to='app.InstagramUser')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='Organization',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('instagram_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='app.InstagramUser')),
            ],
        ),
        migrations.AddField(
            model_name='instagrampost',
            name='instagram_user',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='posts', to='app.InstagramUser'),
        ),
    ]
