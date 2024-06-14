#!/usr/bin/env python3
import collections
import json
import os
import re
import logging
import datetime

from aiohttp import ClientConnectionError
from aiotg import Bot, Chat, RETRY_TIMEOUT
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

queue = asyncio.Queue()
tasks_by_chats = collections.defaultdict(set)


def future_month(date, today):
    if date < today:
        if today.month == 12:
            date = date.replace(month=1, year=date.year + 1)
        else:
            date = date.replace(month=today.month + 1)
    return date


def future_year(date, today):
    if date < today:
        date = date.replace(year=date.year + 1)
    return date


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
            if issubclass(exc_type, asyncio.CancelledError):
                await self.chat.send_text("Фоновая задача была отменена")
            else:
                self.exception = exc_val
                await self.chat.send_text("Ошибка: %s" % str(exc_val))
                logger.exception('Exception: %s', repr(exc_val))


class SeatFilter:
    def __init__(self, only_bottom: bool = False, only_top: bool = False, no_side: bool = False,
                 same_coupe: bool = False):
        self.only_bottom = only_bottom
        self.only_top = only_top
        self.no_side = no_side
        self.same_coupe = same_coupe

    def __str__(self):
        return ', '.join(filter(None, (
            'Только нижние' if self.only_bottom else '',
            'Только верхние' if self.only_top else '',
            'Без боковых' if self.no_side else '',
            'В одном купе' if self.same_coupe else '',
        )))


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

        seats_filter = {}
        if re.search(r'\s+[Нн][Ии][ЗзЖж]', when):
            seats_filter['only_bottom'] = True
            when = re.sub(r'\s+[Нн][Ии][ЗзЖж].*?\b', '', when, re.I)
        if re.search(r'\s+[Вв][Ee][Рр][Хх].*?\b', when):
            seats_filter['only_top'] = True
            when = re.sub(r'\s+[Вв][Ee][Рр][Хх].*?\b', '', when, re.I)
        if re.search(r'\s+[Нн][Ee]\s*[Бб][Оо][Кк].*?\b', when):
            seats_filter['no_side'] = True
            when = re.sub(r'\s+[Нн][Ee]\s*[Бб][Оо][Кк].*?\b', '', when, re.I)

        if re.search(r'\s+[Оо][Дд][Нн][Оо](\s*[Кк][Уу]).*?\b', when):
            seats_filter['same_coupe'] = True
            when = re.sub(r'\s+[Оо][Дд][Нн][Оо](\s*[Кк][Уу]).*?\b', '', when, re.I)

        self.time_range = self.parse_when(when.strip())
        self.min_tickets = min_tickets
        self.types_filter = None
        self.seats_filter = SeatFilter(**seats_filter) if seats_filter else None

    def __str__(self):
        return '{} –> {}, c {} по {}{}{}{}{}'.format(
            self.city_from,
            self.city_to,
            self.time_range.start,
            self.time_range.end,
            ' не дороже {} рублей'.format(self.max_price)
            if self.max_price else '',
            ' только {}'.format(",".join(self.types_filter))
            if self.types_filter else '',
            ' не меньше {} мест в одном поезде'.format(self.min_tickets)
            if self.min_tickets else '',
            str(self.seats_filter) if self.seats_filter else '',
        )

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
            start = future_year(start, today)
            end = future_year(end, today)
            if (end - start).days > 7:
                raise TooLongPeriod('Too long period, use at max 7 days')
            return TimeRange(start, end)

        r = re.match(r'0?(\d+)\.0?(\d+)(?:\s*[-–]\s*(\d+)\.0?(\d+))?', s)
        if r:
            start = datetime.datetime(
                today.year,
                int(r.group(2)),
                int(r.group(1)),
                0,
                0,
            )
            start = future_month(start, today)

            end = start.replace(hour=23, minute=59)
            if r.group(3) is not None:
                end = end.replace(day=int(r.group(3)), month=int(r.group(4)))
            if end < start:
                if start.month == 12:
                    end = end.replace(month=1, year=end.year + 1)
                else:
                    end = end.replace(month=end.month + 1)

            if abs((end - start).days) > 7:
                raise TooLongPeriod('Too long period, use at max 7 days')
            return TimeRange(start, end)

        r = re.match(r'0?(\d+)(?:\s*[\-–.]\s*0?(\d+))?', s)
        if r:
            start = datetime.datetime(
                today.year,
                int(r.group(2)),
                int(r.group(1)),
                0,
                0,
            )
            start = future_year(start, today)
            end = start.replace(hour=23, minute=59)
            if end < start:
                if start.month == 12:
                    end = end.replace(month=1, year=end.year + 1)
                else:
                    end = end.replace(month=end.month + 1)

            if abs((end - start).days) > 7:
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
            start = future_month(start, today)

            end = start.replace(hour=23, minute=59)
            if r.group(2) is not None:
                end = end.replace(day=int(r.group(2)))
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


class QueueItem:
    counter = 1

    def __init__(self, chat: Chat, query: QueryString, start_time=None,
                 deadline=None, city_from=None, city_to=None):
        self.id = self.counter
        self.__class__.counter += 1
        self.chat = chat
        self.start_time = start_time or datetime.datetime.now()
        self.deadline = deadline
        self.query = query
        self.last_call = None
        self.last_notify = self.start_time
        self.city_from = city_from
        self.city_to = city_to

    def __str__(self):
        return '{} –> {}, c {} по {}{}{}{}'.format(
            self.city_from,
            self.city_to,
            self.query.time_range.start.strftime("%Y-%m-%d %H:%M"),
            self.query.time_range.end.strftime("%Y-%m-%d %H:%M"),
            ' не дороже {} рублей'.format(self.query.max_price)
            if self.query.max_price else '',
            ' только {}'.format(",".join(self.query.types_filter))
            if self.query.types_filter else '',
            ' не меньше {} мест в одном поезде'.format(self.query.min_tickets)
            if self.query.min_tickets else '',
        )


async def get_trains(fetcher: RzdFetcher, query: QueryString):
    while True:
        try:
            trains = await fetcher.trains(
                query.city_from,
                query.city_to,
                query.time_range,
            )
            break
        except (UpstreamError, ClientConnectionError):
            await asyncio.sleep(0.5)
            continue

    # exact filtering by time range
    filtered_trains = [
        t
        for t in RzdFetcher.filter_trains(trains, query.types_filter)
        if query.time_range.start <= t.departure_time <= query.time_range.end
    ]

    if query.max_price:
        filtered_trains = filter(lambda t: any(
            s for s in t.seats.values()
            if s.price < query.max_price and (
                not query.min_tickets or s.quantity >= query.min_tickets
            )
        ), filtered_trains)
    elif query.min_tickets:
        filtered_trains = filter(lambda t: any(
            s for s in t.seats.values()
            if s.quantity >= query.min_tickets
        ), filtered_trains)

    if query.seats_filter:
        result = []
        for t in filtered_trains:
            carriages = await fetcher.get_train_carriages(
                t.content['code0'],
                t.content['code1'],
                t.departure_time,
                t.number,
            )
            cars = carriages['lst'][0]['cars']
            valid_cars = []
            for c in cars:
                seat_groups = c['seats']
                if query.seats_filter.only_bottom:
                    seat_groups = [g for g in seat_groups if g['type'] in {'dn'}]
                if query.seats_filter.only_top:
                    seat_groups = [g for g in seat_groups if g['type'] in {'up'}]
                seats = []
                for g in seat_groups:
                    seats.extend(int(s[:3], 10) for s in g['places'].split(','))
                if query.seats_filter.only_bottom:
                    # 'dn' responds all seats, not only down
                    seats = [s for s in seats if s % 2]
                if query.seats_filter.no_side:
                    seats = [s for s in seats if s <= 36]

                car_is_valid = True
                if query.seats_filter.same_coupe:
                    car_is_valid = False
                    for x in range(9):
                        if len({x*4 + 1, x*4 + 2, x*4 + 3, x*4 + 4} & set(seats)) >= 2:  # TODO: remove hardcode
                            car_is_valid = True
                if car_is_valid:
                    valid_cars.append(c)

            if valid_cars:
                result.append(t)
        filtered_trains = result

    return list(filtered_trains), trains


async def process_queue():
    async with RzdFetcher() as fetcher:
        while True:
            try:
                now = datetime.datetime.now()
                while True:
                    task: QueueItem = await queue.get()
                    if task in tasks_by_chats[task.chat.id]:
                        break

                task.last_call = now
                async with NotifyExceptions(task.chat):
                    logger.debug(f'Fetch data for {task.query}')
                    filtered_trains, all_trains = await get_trains(fetcher,
                                                                   task.query)
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
                                        " - %s" % s
                                        for s in train.seats.values()
                                    ),
                                )
                        if len(filtered_trains) > 30:
                            answer += \
                                'Есть ещё поезда, сократите диапазон дат... '
                        tasks_by_chats[task.chat.id].remove(task)
                        await task.chat.send_text(answer, parse_mode='HTML')
                    else:
                        now = datetime.datetime.now()
                        if task.deadline and now > task.deadline:
                            await task.chat.send_text(
                                'Ничего не нашёл. Прекращаю работу.')
                        else:
                            await queue.put(task)
                            if (now - task.last_notify).seconds > 3600:
                                task.last_notify = now
                                time_start = task.query.time_range.start.\
                                    strftime("%Y-%m-%d %H:%M")
                                time_end = task.query.time_range.end.\
                                    strftime("%Y-%m-%d %H:%M")
                                await task.chat.send_text(
                                    'Всё ещё нет билетов '
                                    '{city_from} – {city_to} '
                                    '{time_start} - {time_end}. '
                                    'Ищу уже {working} секунд.\n'
                                    'Продолжаю поиск...'.format(
                                        city_from=task.city_from,
                                        city_to=task.city_to,
                                        time_start=time_start,
                                        time_end=time_end,
                                        working=(
                                            now - task.start_time
                                        ).seconds,
                                    ),
                                )
            except asyncio.CancelledError:
                raise
            # except:  # noqa
            #     pass
            try:
                logger.debug('Sleep for 30 seconds')
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return


@multibot('notify')
async def notify(chat: Chat, match):
    user = await chat.get_chat_member(chat.sender["id"])
    logger.info('notify {}'.format(user['result']['user']))

    async with NotifyExceptions(chat) as notifier:
        query = QueryString(match)
    if notifier.exception:
        return

    async with RzdFetcher() as fetcher:
        async with NotifyExceptions(chat) as notifier:
            city_from = (
                await fetcher.get_city_autocomplete(query.city_from)
            )['n']
            city_to = (await fetcher.get_city_autocomplete(query.city_to))['n']
        if notifier.exception:
            return

        msg = """Буду искать по запросу {} -> {}, с {} по {}{}{}{}{}""".format(
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
            query.seats_filter or '',
        )
        await chat.send_text(msg)
        start_time = datetime.datetime.now()
        task = QueueItem(
            chat,
            query,
            start_time=start_time,
            deadline=start_time + datetime.timedelta(days=1),
            city_from=city_from,
            city_to=city_to,
        )
        tasks_by_chats[chat.id].add(task)
        await queue.put(task)


@multibot('search', default=True)
async def search(chat: Chat, match):
    user = await chat.get_chat_member(chat.sender["id"])
    logger.info('search;{};{}'.format(user['result']['user'], match.group(0)))
    await chat.send_text('Ищу билеты...')

    async with NotifyExceptions(chat) as notifier:
        query = QueryString(match)
    if notifier.exception:
        return

    async with RzdFetcher() as fetcher:
        async with NotifyExceptions(chat) as notifier:
            filtered_trains, all_trains = await get_trains(fetcher, query)
        if notifier.exception:
            return

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


@bot.command('/status')
async def status(chat: Chat, match):
    tasks = tasks_by_chats[chat.id]
    if tasks:
        answer = 'Текущие задачи: \n' + '\n'.join(
            f'- {t} /stop{t.id}' for t in tasks
        )
    else:
        answer = 'Текущих задач нет'
    await chat.send_text(answer, parse_mode='HTML')


@bot.command(r'/stop(\d+)')
async def stop(chat: Chat, match):
    async with NotifyExceptions(chat) as notifier:
        tasks = list(filter(
            lambda x: x.id == int(match.group(1)),
            tasks_by_chats[chat.id]
        ))
        # queue is not filtered, check for presence in dict instead
        tasks_by_chats[chat.id] = {
            x for x in tasks_by_chats[chat.id]
            if x.id != int(match.group(1))
        }
    if notifier.exception:
        return

    if tasks:
        answer = f"Задача отменена.\n{tasks[0]} больше не будет выполняться"
    else:
        answer = 'Нет такой задачи'
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


async def stop_bot():
    while not queue.empty():
        task = await queue.get()
        task.chat.send_text('Bot is quitting. Bye!')


def patch_bot_api_call(bot: Bot):
    original_api_call = bot.api_call

    async def api_call_with_handle_exceptions(method, **params):
        try:
            return await original_api_call(method, **params)
        except ClientConnectionError:
            await asyncio.sleep(RETRY_TIMEOUT)
            return await original_api_call(method, **params)
    bot.api_call = api_call_with_handle_exceptions


if __name__ == '__main__':
    logger.setLevel(logging.DEBUG)
    logger.warning('Start RZD telegram bot...')

    # do not get down on aiohttp exceptions
    patch_bot_api_call(bot)

    loop = asyncio.get_event_loop()
    bot_future = asyncio.ensure_future(bot.loop())
    task_future = asyncio.ensure_future(process_queue())
    try:
        loop.run_until_complete(bot_future)
        # bot.run()
    except KeyboardInterrupt:
        bot_future.cancel()
        task_future.cancel()
        loop.run_until_complete(stop_bot())
        bot.stop()
        # raise
    # except:  # noqa
    #     pass
    finally:
        loop.run_until_complete(bot.session.close())
        logger.debug("Closing loop")
    loop.stop()
    loop.close()
