import os
import sys
import json
import uuid
import tempfile
import posixpath
import traceback
import unicodedata
import multiprocessing
from Queue import Queue
from threading import Thread
from datetime import datetime
from contextlib import contextmanager

from urlparse import urlparse

from werkzeug.http import http_date
from werkzeug.posixemulation import rename
from jinja2 import is_undefined
from markupsafe import Markup


is_windows = sys.platform.startswith('win')


_slash_escape = '\\/' not in json.dumps('/')


class JSONEncoder(json.JSONEncoder):

    def default(self, o):
        if is_undefined(o):
            return None
        if isinstance(o, datetime):
            return http_date(o)
        if isinstance(o, uuid.UUID):
            return str(o)
        if hasattr(o, '__html__'):
            return unicode(o.__html__())
        return json.JSONEncoder.default(self, o)


def htmlsafe_json_dump(obj, **kwargs):
    kwargs.setdefault('cls', JSONEncoder)
    rv = json.dumps(obj, **kwargs) \
        .replace(u'<', u'\\u003c') \
        .replace(u'>', u'\\u003e') \
        .replace(u'&', u'\\u0026') \
        .replace(u"'", u'\\u0027')
    if not _slash_escape:
        rv = rv.replace('\\/', '/')
    return rv


def tojson_filter(obj, **kwargs):
    return Markup(htmlsafe_json_dump(obj, **kwargs))


def safe_call(func, args=None, kwargs=None):
    try:
        return func(*(args or ()), **(kwargs or {}))
    except Exception:
        # XXX: logging
        traceback.print_exc()


class Worker(Thread):

    def __init__(self, tasks):
        Thread.__init__(self)
        self.tasks = tasks
        self.daemon = True
        self.start()

    def run(self):
        while 1:
            func, args, kwargs = self.tasks.get()
            safe_call(func, args, kwargs)
            self.tasks.task_done()


class WorkerPool(object):

    def __init__(self, num_threads=None):
        if num_threads is None:
            num_threads = multiprocessing.cpu_count()
        self.tasks = Queue(num_threads)
        for _ in range(num_threads):
            Worker(self.tasks)

    def add_task(self, func, *args, **kargs):
        self.tasks.put((func, args, kargs))

    def wait_for_completion(self):
        self.tasks.join()


def slugify(value):
    # XXX: not good enough
    return u'-'.join(value.strip().split()).lower()


class Url(object):

    def __init__(self, value):
        self.url = value
        self.host = urlparse(value).netloc

    def __unicode__(self):
        return self.url

    def __str__(self):
        return self.url


def is_unsafe_to_delete(path, base):
    a = os.path.abspath(path)
    b = os.path.abspath(base)
    diff = os.path.relpath(a, b)
    first = diff.split(os.path.sep)[0]
    return first in (os.path.curdir, os.path.pardir)


def prune_file_and_folder(name, base):
    if is_unsafe_to_delete(name, base):
        return False
    try:
        os.remove(name)
    except OSError:
        try:
            os.rmdir(name)
        except OSError:
            return False
    head, tail = os.path.split(name)
    if not tail:
        head, tail = os.path.split(head)
    while head and tail:
        try:
            if is_unsafe_to_delete(head, base):
                return False
            os.rmdir(head)
        except OSError:
            break
        head, tail = os.path.split(head)
    return True


def sort_normalize_string(s):
    return unicodedata.normalize('NFD', unicode(s).lower().strip())


def get_dependent_url(url_path, suffix):
    url_directory, url_filename = posixpath.split(url_path)
    url_base, url_ext = posixpath.splitext(url_filename)
    return posixpath.join(url_directory, url_base + u'@' + suffix + url_ext)


@contextmanager
def atomic_open(filename, mode='r'):
    if 'r' not in mode:
        fd, tmp_filename = tempfile.mkstemp(
            dir=os.path.dirname(filename), prefix='.__atomic-write')
        f = os.fdopen(fd, mode)
    else:
        f = open(filename, mode)
        tmp_filename = None
    try:
        yield f
    except:
        f.close()
        exc_type, exc_value, tb = sys.exc_info()
        if tmp_filename is not None:
            try:
                os.remove(tmp_filename)
            except OSError:
                pass
        raise exc_type, exc_value, tb
    else:
        f.close()
        if tmp_filename is not None:
            rename(tmp_filename, filename)
