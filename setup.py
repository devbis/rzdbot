#!/usr/bin/env python

from distutils.core import setup

setup(
    name='rzdbot',
    version='0.1',
    description='Telegram bot for searching tickets on rzd.ru',
    author='Ivan Belokobylskiy',
    author_email='belokobylskij@gmail.com',
    url='https://github.com/devbis/rzdbot/',
    py_modules=['rzdbot'],
    classifiers=[
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3 :: Only',
        'Topic :: Communications :: Chat',
    ],
)
