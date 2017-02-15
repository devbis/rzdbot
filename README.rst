Telagram bot for searching tickets at rzd.ru


Requirements
============
rzdbot requires python 3.5 or higher due to async/await syntax and aiorzd
library


Installation
============
I asssume you are already register telegram bot. Use the manual for help
https://core.telegram.org/bots

Make sure you have installed aiorzd. Then type ::

    sudo python ./setup.py install


Use config.json as a sample to add bot's name and API token for accessing
Telegram. ::

    {
      "API_TOKEN": "YOUR_TOKEN",
      "BOT_NAME": "MyRZDBot"
    }

Run bot
-------
::

    $ python3 -m rzdbot

or::

    $ python3 rzdbot.py

You can pass path to config.json via environment variable::

    $ BOT_CONFIG=/etc/rzdbot.json python3 -m rzdbot

Usage
=====

You can search tickets with search command ::

    /search Москва, Санкт-Петербург, 20.02 < 2000

will search for trains on february 20 with max price 2000 rubles.
If you need ticket for a group of people, you can set minimal tickets limit ::

    /search Москва, Санкт-Петербург, 20.02 < 2000 #3

Also some shortcuts supported: ::

    /search мск спб 20 < 2000

will search for tickets on closest 20th day cheaper than 2000 rubles
