#!/usr/bin/env python3
"""
Reads the recorded sessions of the courses students follow and writes lectures.json, which the apps
read from GitHub, so the university site only ever sees this one slow visitor.

One request at a time, two seconds apart. If the site struggles the checker waits longer, and it
stops for this round after repeated failures; the courses it did not reach keep their last result.
"""
import http.cookiejar
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

BASE = 'http://sessions.svuonline.org/new/'
POST_URL = BASE + 'Default.aspx'
OUTPUT = 'lectures.json'
USER_AGENT = 'ACADEX-checker/1.0'
FIELDS = ['DropDownList_Term', 'DropDownList_Program', 'DropDownList_Course', 'DropDownList_Tutor', 'DropDownList_Class']
TERM, PROGRAM, COURSE, TUTOR, CLASS = FIELDS

DELAY = 2.0
RETRY_WAITS = [10, 30, 90]
STOP_AFTER_FAILURES = 3
TIME_BUDGET = 40 * 60
SESSIONS_PER_CLASS = 10
TOUCH_AFTER_DAYS = 25    # GitHub pauses scheduled jobs in a repository that stays quiet for 60 days
MAX_COURSES = 2000


class Page(HTMLParser):
    """The parts of the ASP.NET page the checker needs: hidden fields, drop-downs and the sessions grid."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.hidden, self.selects, self.rows = {}, {}, []
        self._select = self._option = self._text = self._row = self._cell = None
        self._in_grid = False
        self._depth = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'input' and (a.get('type') or '').lower() == 'hidden' and a.get('name'):
            self.hidden[a['name']] = a.get('value') or ''
        elif tag == 'select':
            self._select = a.get('name')
            self.selects[self._select] = []
        elif tag == 'option' and self._select:
            self._option, self._text = a.get('value') or '', ''
        elif tag == 'table':
            if a.get('id') == 'GridView_results':
                self._in_grid, self._depth = True, 0
            if self._in_grid:
                self._depth += 1
        elif self._in_grid and tag == 'tr':
            self._row = []
        elif self._in_grid and tag == 'td' and self._row is not None:
            self._cell = ''

    def handle_endtag(self, tag):
        if tag == 'option' and self._option is not None:
            if self._option not in ('', '0'):
                self.selects[self._select].append((self._option, self._text.strip()))
            self._option = None
        elif tag == 'select':
            self._select = None
        elif self._in_grid and tag == 'td' and self._cell is not None:
            self._row.append(self._cell.strip())
            self._cell = None
        elif self._in_grid and tag == 'tr':
            if self._row and len(self._row) >= 8:
                self.rows.append(self._row)
            self._row = None
        elif self._in_grid and tag == 'table':
            self._depth -= 1
            if self._depth == 0:
                self._in_grid = False

    def handle_data(self, data):
        if self._option is not None:
            self._text += data
        elif self._cell is not None:
            self._cell += data


class TooManyFailures(Exception):
    pass


class Site:
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.page = None
        self.chosen = {}
        self.requests = 0
        self.failures = 0

    def _fetch(self, data=None):
        for wait in RETRY_WAITS + [None]:
            time.sleep(DELAY)
            self.requests += 1
            try:
                req = urllib.request.Request(POST_URL if data else BASE, data=data, headers={'User-Agent': USER_AGENT})
                with urllib.request.urlopen(req, timeout=60) as r:
                    html = r.read().decode('utf-8', 'replace')
                self.failures = 0
                return Page(html)
            except OSError as e:
                print('request failed:', e, file=sys.stderr)
                if wait is None:
                    break
                time.sleep(wait)
        self.failures += 1
        if self.failures >= STOP_AFTER_FAILURES:
            raise TooManyFailures()
        raise OSError('request failed')

    def start(self):
        self.chosen = {}
        self.page = self._fetch()
        return self.page.selects.get(TERM, [])

    def choose(self, field, value):
        keep = FIELDS[:FIELDS.index(field)]
        self.chosen = {k: v for k, v in self.chosen.items() if k in keep}
        self.chosen[field] = value
        form = {k: v for k, v in self.page.hidden.items() if k not in ('__EVENTTARGET', '__EVENTARGUMENT')}
        form.update({'__EVENTTARGET': field, '__EVENTARGUMENT': ''})
        form.update(self.chosen)
        self.page = self._fetch(urllib.parse.urlencode(form).encode())
        at = FIELDS.index(field) + 1
        return self.page.selects.get(FIELDS[at], []) if at < len(FIELDS) else []


def read_course(site, term, program, course):
    """Every class of the course, newest sessions first; None if the site no longer offers the course."""
    site.start()
    if not any(v == term for v, _ in site.page.selects.get(TERM, [])):
        return None
    programs = site.choose(TERM, term)
    if not any(v == program for v, _ in programs):
        return None
    courses = site.choose(PROGRAM, program)
    label = next((name for v, name in courses if v == course), None)
    if label is None:
        return None
    sessions = []
    for tutor, _ in site.choose(COURSE, course):
        for klass, _ in site.choose(TUTOR, tutor):
            site.choose(CLASS, klass)
            for row in site.page.rows[:SESSIONS_PER_CLASS]:
                # Columns: select, program, course, class, term, order, tutor, date. Nobody's name is kept.
                _, _, _, class_code, _, order, _, date = row[:8]
                sessions.append({'id': '|'.join((class_code, order, date)), 'class': class_code, 'order': order, 'date': date})
    return {'name': label, 'sessions': sessions}


def followed():
    url = os.environ.get('FOLLOW_URL', '').rstrip('/')
    if not url:
        return []
    with urllib.request.urlopen(urllib.request.Request(url + '/list', headers={'User-Agent': USER_AGENT}), timeout=60) as r:
        return json.load(r)[:MAX_COURSES]


def load():
    try:
        with open(OUTPUT, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {'updated': 0, 'cursor': 0, 'courses': {}}


def save(data):
    with open(OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def main():
    wanted = followed()
    previous = load()
    keys = ['|'.join((c['term'], c['program'], c['course'])) for c in wanted]
    courses = {k: v for k, v in previous.get('courses', {}).items() if k in keys}
    changed = set(courses) != set(previous.get('courses', {}))
    site = Site()
    started = time.time()
    cursor = previous.get('cursor', 0)
    # Each round starts where the last one stopped, so every course gets its turn.
    done = 0
    try:
        for i in range(len(wanted)):
            if time.time() - started > TIME_BUDGET:
                break
            c = wanted[(cursor + i) % len(wanted)]
            key = '|'.join((c['term'], c['program'], c['course']))
            try:
                result = read_course(site, c['term'], c['program'], c['course'])
            except OSError:
                done += 1
                continue
            done += 1
            if result is not None and result != courses.get(key):
                courses[key] = result
                changed = True
    except TooManyFailures:
        print('the site is not answering; stopping this round', file=sys.stderr)

    now = int(time.time() * 1000)
    stale = now - previous.get('updated', 0) > TOUCH_AFTER_DAYS * 24 * 3600 * 1000
    print('courses read: %d of %d, requests: %d, changed: %s' % (done, len(wanted), site.requests, changed))
    if changed or stale:
        save({'updated': now, 'cursor': (cursor + done) % max(1, len(wanted)), 'courses': courses})


def probe():
    """Shows the shape of the site with a handful of requests, without writing anything."""
    site = Site()
    terms = site.start()
    print('terms', terms[:3])
    programs = site.choose(TERM, terms[0][0])
    print('programs', len(programs), programs[:3])
    courses = site.choose(PROGRAM, programs[0][0])
    print('courses', len(courses), courses[:3])
    result = read_course(site, terms[0][0], programs[0][0], courses[0][0])
    print('sessions', result and (result['name'], len(result['sessions']), result['sessions'][:2]))
    print('requests', site.requests)


if __name__ == '__main__':
    probe() if '--probe' in sys.argv else main()
