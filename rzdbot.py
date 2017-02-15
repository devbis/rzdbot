#!/usr/bin/env python3

import json
import os
import re
import logging
import datetime

from aiotg import Bot, Chat
from aiorzd import TimeRange, RzdFetcher, UpstreamError
import asyncio

logger = logging.getLogger('aiorzd_bot')
os.environ.setdefault('BOT_CONFIG', 'config.json')

with open(os.environ['BOT_CONFIG']) as cfg:
    config = json.load(cfg)

bot = Bot(config['API_TOKEN'], name=config['BOT_NAME'])


shortcuts = {
    'москва': [
        'мск',
        'м',
    ],
    'санкт-петербург': [
        'спб',
        'сп',
        'п',
        'с',
    ],
}

QUERY_REGEXP_LIST = [
    r'(?P<from>[^,]+)\s*,\s*(?P<to>[^,]+)\s*,\s*(?P<when>.*)',
    r'(?P<from>[^\s]+)\s+(?P<to>[^\s]+)(?P<when>.*)',
]


def multibot(command, default=False):
    def decorator(fn):
        for r in QUERY_REGEXP_LIST:
            fn = bot.command(r'/%s@%s\s+%s' % (command, bot.name, r))(fn)
            fn = bot.command(r'/%s\s+%s' % (command, r))(fn)
            if default:
                fn = bot.command(r'@%s\s+%s' % (bot.name, r))(fn)

        return fn
    return decorator


class TooLongPeriod(Exception):
    pass


class NotifyExceptions:
    def __init__(self, chat):
        self.chat = chat
        self.exception = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_val:
            self.exception = exc_val
            await self.chat.send_text("Ошибка: %s" % str(exc_val))
            logger.error('Exception: %s', repr(exc_val))


class QueryString:
    def __init__(self, match):
        d = {v: k for k, vs in shortcuts.items() for v in vs}
        from_city = match.group('from').strip()
        to_city = match.group('to').strip()
        self.city_from = d.get(from_city, from_city)
        self.city_to = d.get(to_city, to_city)

        # TODO: filtering by wagon type
        max_price = None
        try:
            when = match.group('when').strip()
            if '<' in when:
                when, max_price = when.split('<')
                max_price, min_tickets = self.parse_max_price(max_price)
            else:
                when, min_tickets = self.parse_max_price(when)
        except ValueError:
            when = None
            min_tickets = None

        self.max_price = None
        if max_price and max_price.strip():
            try:
                self.max_price = int(max_price.strip())
            except ValueError:
                pass

        self.time_range = self.parse_when(when.strip())
        self.min_tickets = min_tickets
        self.types_filter = None

    @staticmethod
    def parse_max_price(max_price):
        if '#' in max_price:
            max_price, required_tickets = max_price.split('#')
            return max_price.strip(), int(required_tickets.strip())
        return max_price, None

    @staticmethod
    def parse_when(s):
        today = datetime.datetime.now().replace(hour=0, minute=0, second=0,
                                                microsecond=0)

        if not s:
            now = datetime.datetime.now()
            return TimeRange(now, now + datetime.timedelta(days=1))
        r = re.match(
            r'0?(\d+)[./]0?(\d+)\s+0?(\d+):0?(\d+)'
            r'\s*[-–]\s*'
            r'0?(\d+)[./]0?(\d+)\s+0?(\d+):0?(\d+)',
            s,
        )
        if r:
            start = datetime.datetime(
                today.year,
                int(r.group(2)),
                int(r.group(1)),
                int(r.group(3)),
                int(r.group(4)),
            )
            end = datetime.datetime(
                today.year,
                int(r.group(6)),
                int(r.group(5)),
                int(r.group(7)),
                int(r.group(8)),
            )
            if start < today:
                start = start.replace(year=today.year + 1)
                end = end.replace(year=today.year + 1)
                if (end - start).days > 7:
                    raise TooLongPeriod('Too long period, use at max 7 days')
            return TimeRange(start, end)
        r = re.match(r'0?(\d+)(?:\s*[-–]\s*(\d+))?', s)
        if r:
            start = datetime.datetime(
                today.year,
                today.month,
                int(r.group(1)),
                0,
                0,
            )

            if start < today:
                if today.month == 12:
                    start = start.replace(month=1, year=start.year + 1)
                else:
                    start = start.replace(month=today.month + 1)

            end = start.replace(hour=23, minute=59)
            if r.group(2) is not None:
                end = end.replace(
                    day=int(r.group(2))
                )
            if end < start:
                if start.month == 12:
                    end = end.replace(month=1, year=end.year + 1)
                else:
                    end = end.replace(month=end.month + 1)

            if abs((end - start).days) > 7:
                raise TooLongPeriod('Too long period, use at max 7 days')
            return TimeRange(start, end)

        logger.error('Cannot parse date range "%s"', s)
        raise ValueError('Не понял диапазон дат...')


async def get_trains(fetcher: RzdFetcher, query: QueryString):
    while True:
        try:
            trains = await fetcher.trains(
                query.city_from,
                query.city_to,
                query.time_range,
            )
            break
        except UpstreamError:
            await asyncio.sleep(0.5)
            continue

    filtered_trains = RzdFetcher.filter_trains(trains, query.types_filter)

    if query.max_price:
        filtered_trains = filter(lambda t: any(
            s for s in t.seats.values()
            if s.price < query.max_price and (
                not query.min_tickets or s.quantity >= query.min_tickets
            )
        ), trains)

    return list(filtered_trains), trains


@multibot('notify')
async def notify(chat: Chat, match):
    user = await chat.get_chat_member(chat.sender["id"])
    logger.info('notify {}'.format(user['result']['user']))

    async with NotifyExceptions(chat) as notifier:
        query = QueryString(match)
    if notifier.exception:
        return

    fetcher = RzdFetcher()
    async with NotifyExceptions(chat) as notifier:
        city_from = (await fetcher.get_city_autocomplete(query.city_from))['n']
        city_to = (await fetcher.get_city_autocomplete(query.city_to))['n']
    if notifier.exception:
        return

    msg = """Буду искать по запросу {} -> {}, с {} по {}{}{}{}""".format(
        city_from,
        city_to,
        query.time_range.start,
        query.time_range.end,
        ' не дороже {} рублей'.format(query.max_price)
        if query.max_price else '',
        ' только {}'.format(",".join(query.types_filter))
        if query.types_filter else '',
        ' не меньше {} мест в одном поезде'.format(query.min_tickets)
        if query.min_tickets else '',
    )
    await chat.send_text(msg)
    start_time = datetime.datetime.now()
    last_notify = start_time

    async with NotifyExceptions(chat):
        while True:
            filtered_trains, all_trains = await get_trains(fetcher, query)
            if filtered_trains:
                answer = 'Найдено: \n'
                for train in filtered_trains[0:30]:
                    answer += \
                        '<b>{date}</b>\n' \
                        '<i>{num} {title}</i>\n' \
                        '{seats}\n\n'.format(
                            date=train.departure_time,
                            num=train.number,
                            title=train.title,
                            seats="\n".join(
                                " - %s" % s for s in train.seats.values()
                            ),
                        )
                if len(filtered_trains) > 30:
                    answer += 'Есть ещё поезда, сократите диапазон дат... '

                await chat.send_text(answer, parse_mode='HTML')
                break
            else:
                logger.info('sleep for 30 sec')
                await asyncio.sleep(30)

            now = datetime.datetime.now()
            if (now - start_time).seconds > 86400:
                await chat.send_text('Ничего не нашёл. Прекращаю работу.')
                break
            elif (now - last_notify).seconds > 3600:
                last_notify = now
                await chat.send_text('Всё ещё нет билетов. Ищу...')


@multibot('search', default=True)
async def search(chat: Chat, match):
    user = await chat.get_chat_member(chat.sender["id"])
    logger.info('search;{};{}'.format(user['result']['user'], match.group(0)))
    await chat.send_text('Ищу билеты...')

    async with NotifyExceptions(chat) as notifier:
        query = QueryString(match)
    if notifier.exception:
        return

    filtered_trains, all_trains = await get_trains(RzdFetcher(), query)

    if not filtered_trains:
        if all_trains:
            answer = 'По запросу поездов нет, но есть более дорогие'
        else:
            answer = 'По вашему запросу поездов нет'
    else:
        answer = 'Найдено: \n'
        for train in filtered_trains[0:30]:
            answer += \
                '<b>{date}</b>\n' \
                '<i>{num} {title}</i>\n' \
                '{seats}\n\n'.format(
                    date=train.departure_time,
                    num=train.number,
                    title=train.title,
                    seats="\n".join(
                        " - %s" % s for s in train.seats.values()
                    ),
                )
        if len(filtered_trains) > 30:
            answer += 'Есть ещё поезда, сократите диапазон дат... '

    await chat.send_text(answer, parse_mode='HTML')


@bot.default
def default(chat: Chat, match):
    logger.warning('Not matched request: {}'.format(match))
    return chat.send_text('Не понял...')


@bot.command("(/start|/?help)")
def usage(chat: Chat, match):
    demo_date = datetime.date.today() + datetime.timedelta(days=30)
    logger.info('Start request: {}'.format(match))
    text = """
Привет! Я умею искать билеты на поезд.
Как спросить у меня список билетов:
/search москва, спб, 4.{month:02d} 20:00 - 5.{month} 03:00
    """.format(month=demo_date.month)
    return chat.send_text(text)


if __name__ == '__main__':
    logger.setLevel(logging.INFO)
    logger.warning('Start RZD telegram bot...')
    bot.run()
