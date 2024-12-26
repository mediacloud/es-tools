#!/usr/bin/env python3

"""
Phil Budne @ Media Cloud, started Nov 7, 2024

This is a "top" program for Elasticsearch, showing the active
tasks ordered by CPU usage, in the spirit of
the Un*x "top" program (original by William LeFebvre c. 1984)
and pg_top (https://pg_top.gitlab.io/), and also the
4.3BSD "systat" program.

Caveats:

I'm not a DB/ES person!

Developed with Python 3.10 & elasticsearch 8.12.1
against ES 8.15

Currently only displays the totals returned, and not the incremental
changes since last display.

WILL display raw queries!!
"""
# XXX toggle for one banner line per node!

import curses
import json
import os
import sys
import time
import warnings
from enum import Enum
from types import ModuleType
from typing import Any, Callable, NamedTuple, NoReturn, TypedDict, cast

import elasticsearch

termios: ModuleType | None = None
msvcrt: ModuleType | None = None
try:
    import termios

    IFLAG, OFLAG, CFLAG, LFLAG, ISPEED, OSPEED, CC = 0, 1, 2, 3, 4, 5, 6
except ImportError:
    try:
        import msvcrt
    except ImportError:
        pass

# Suppress "GeneralAvailabilityWarning: This API is in technical
# preview and may be changed or removed in a future release. Elastic
# will work to fix any issues, but features in technical preview are
# not subject to the support SLA of official GA features." for the
# task API.
try:
    warnings.filterwarnings(
        "ignore", category=elasticsearch.exceptions.GeneralAvailabilityWarning
    )
except AttributeError:
    pass

DISPLAY_INTERVAL = 5.0
# US = "μs"      # curses wants .UTF-8 in locale!
US = "us"
JSON = dict[str, Any]


class Show(Enum):
    """
    task display filter
    """

    NORMAL = "n"
    ALL = "a"  # tasks w/o description, but not persistent
    PERSISTENT = "p"


SHOW_BY_VALUE = {x.value: x for x in Show}


class TaskDict(TypedDict):
    action: str
    children: list["TaskDict"]
    headers: dict[str, str]
    id: str
    node: str
    running_time_in_nanos: int
    start_time_in_millis: int
    type: str

    # added:
    _descr: str
    _full_data: JSON
    _max_age: float
    _task_cpu_percent: float
    _total_cpu_percent: float
    _total_elapsed: float
    _total_runtime: float
    _total_tasks: int


def get_path(data: JSON, path: str, default: Any = None) -> Any:
    """
    convenience function to extract a value from JSON using a JS-ish
    path string (takes int values w/o []).
    """
    j = data
    try:
        for item in path.split("."):
            if j is None:
                return default

            assert isinstance(j, (dict, list))
            if isinstance(j, list):
                j = j[int(item)]  # may throw ValueError
            else:
                j = j[item]
        return j
    except ValueError:
        # here when query shape different
        # than a query-decoder expects
        # (debug log message?)
        return default
    except (KeyError, TypeError):
        return default


class ESTaskGetter:
    """
    class with method(s) to retrieve Elastic Search task data
    """

    def __init__(self) -> None:
        self._reset()
        self.debug = False

        # options/keystrokes to enable these?

        self.show = Show.NORMAL
        self.show_task_count = False  # show tasks & avg run%
        self.raw_descr = False  # display raw queries/documents
        self.prefer_opaque_id = False  # display instead of query
        self.show_age = False  # display instead of run%
        self.show_individuals = False  # instead of just parent

    def _reset(self) -> None:
        self.trees: list[TaskDict] = []  # the roots
        self._start = 0.0

    def set_urls(self, hosts: str) -> None:
        self.es = elasticsearch.Elasticsearch(
            hosts.split(","), opaque_id=self.create_opaque_id()
        )

    @staticmethod
    def _get_user() -> str | None:
        """
        get current user for opaque id generation
        """
        try:
            # libc getlogin returns user logged in on the
            # controlling terminal of the current process
            # (ie; fails in a daemonize'd process)
            return os.getlogin()
        except OSError:
            uid = os.getuid()  # so many to choose from!
            try:
                import pwd

                pwent = pwd.getpwuid(uid)
                return pwent.pw_name
            except (ImportError, KeyError):
                user = os.environ.get("USER")  # may be a lie
                if not user:
                    user = f"uid{str(uid)}"
                return user
        return None

    def create_opaque_id(self) -> str:
        """
        create opaque_id for client; here for override.
        WILL be returned in data from ESQueryGetter
        (esp. for this process!!)
        """
        # https://www.elastic.co/guide/en/elasticsearch/reference/current/api-conventions.html
        # says:
        #   The X-Opaque-Id header accepts any arbitrary
        #   value. However, we recommend you limit these values to a
        #   finite set, such as an ID per client. Don’t generate a
        #   unique X-Opaque-Id header for every request. Too many
        #   unique X-Opaque-Id values can prevent Elasticsearch from
        #   deduplicating warnings in the deprecation logs.
        # Which probaly REALLY means one string per client LIBRARY!

        cname = type(self).__name__  # class name
        return cname

    def _process_tasks(self, tad: dict[str, TaskDict]) -> None:
        """
        takes dict indexed by task_id
        """
        for task_id, _td in tad["tasks"].items():
            task_data = cast(TaskDict, _td)
            task_type = task_data["type"]
            full_data = None

            if task_type == "persistent" and self.show != Show.PERSISTENT:
                continue

            if task_type != "persistent":  # maybe others?
                try:
                    # full_data["task"] == task_data + description & status??
                    # also full_data["completed"] (bool)
                    # so COULD just replace task_data here?
                    full_data = self.es.tasks.get(task_id=task_id)
                except elasticsearch.ApiError:
                    pass

            if full_data:
                task_data["_full_data"] = full_data.raw  # JSON dumpable
            else:
                # action starting with "cluster:monitor" may be this program
                # or another monitoring agent
                if not self.get_opaque_id(task_data) and self.show == Show.NORMAL:
                    continue

            self.trees.append(task_data)

    def get_opaque_id(self, t: TaskDict) -> str:
        """
        get opaque-id (passed as query arg on request URL,
        generated by self.create_opaque_id for this code)
        from a TaskDict
        """
        return t.get("headers", {}).get("X-Opaque-Id") or ""

    def get_tasks(self) -> None:
        """
        collect task list, then detail ASAP
        """
        self._reset()
        assert self.es
        if self.show_individuals:
            # collect individual tasks, not trees: add a toggle?!
            for n in self.es.tasks.list()["nodes"].values():
                self._process_tasks(n)
        else:
            # not broken down by node, parents have "children" arrays
            self._process_tasks(
                cast(dict[str, TaskDict], self.es.tasks.list(group_by="parents"))
            )

        self._start = time.time()  # before can result in negatives

    def total_times(self) -> None:
        """
        OPTIONALLY annotate current trees with totals of runtime, elapsed
        and tasks for each node and all children.  This is based on
        the ASSumption that parent runtime doesn't reflect child times.
        """
        for t in self.trees:
            self._total_times(t)

    def _total_times(self, task: TaskDict) -> None:
        """
        worker for depth-first traversal to total tasks, times
        """
        # init totals for this task & subtree
        task["_total_tasks"] = 1  # tasks in this subtree

        # times in seconds: this is the one place that does time conversions
        r = task["_total_runtime"] = task["running_time_in_nanos"] / 1e9
        e = task["_total_elapsed"] = task["_max_age"] = max(
            self._start - task["start_time_in_millis"] / 1000, 0
        )

        # I avoid the Python trinary, but I'll make this one exception
        # (pun intended):
        task["_task_cpu_percent"] = task["_total_cpu_percent"] = (
            100 * r / e if e else 0.0
        )

        # sum times for children
        for child in task.get("children", []):
            self._total_times(child)  # depth first

            # add child totals into ours:
            task["_total_tasks"] += child["_total_tasks"]
            task["_total_runtime"] += child["_total_runtime"]
            task["_total_cpu_percent"] += child["_total_cpu_percent"]

            task["_total_elapsed"] += child["_total_elapsed"]
            # children can be OLDER?!
            task["_max_age"] = max(task["_max_age"], child["_max_age"])


################


def format_interval(secs: float) -> str:
    """
    format time in seconds to fit in 6 chars or less
    """

    def ffrac(x: float) -> str:
        """
        return number formatted
        to fit in four characters
        (so two unit chars can be added)
        """
        if x >= 100:
            return f"{int(x)}"  # xxx
        if x >= 10:
            return f"{x:.1f}"  # xx.x
        # x.xx
        return f"{x:.2f}"

    # possible, in theory, for runtime
    if secs < 0.000001:
        # max 999ns
        return f"{ffrac(secs*1e9)}ns"

    if secs < 0.001:
        # max 999μs
        return f"{ffrac(secs*1e6)}{US}"

    # originally didn't have this one
    # (always displayed 0.nnn)
    if secs < 0.1:
        # max 99ms
        return f"{ffrac(secs*1e3)}ms"

    if secs < 100:
        # max 99.999
        return f"{secs:6.3f}"  # SS.mmm

    minutes = int(secs / 60)
    if minutes < 1000:
        # max 999m59
        sec = int(secs % 60)
        return f"{minutes:3d}m{sec:02d}"

    hours = int(minutes / 60)
    minutes %= 60
    if hours < 1000:
        # max 999h59
        return f"{hours}h{minutes:02d}"

    days = int(hours / 24)
    hours %= 24
    if days < 100:
        # max 99d23h
        return f"{days}d{hours}h"

    if days <= 365:
        # max 52w2d
        weeks = days // 7
        days %= 7
        return f"{weeks}w{days}d"

    # good up to 99y366
    years = days // 365
    days %= 365
    return f"{years}y{days}d"


def get_id(t: TaskDict) -> str:
    """
    return short/displayable id
    """
    # just enough info to track a long-running task
    node = t["node"][-4:]
    id = str(t["id"])[-4:]
    return f"{node}.{id}"


def get_runtime(t: TaskDict) -> str:
    """
    return total runtime for task (tree)
    """
    return format_interval(t["_total_runtime"])


def get_age(t: TaskDict) -> str:
    """
    return age of oldest task in tree
    """
    return format_interval(t["_max_age"])


def get_ttl_tasks(t: TaskDict) -> int:
    """
    return task count for task (tree)
    """
    return t["_total_tasks"]


def get_ttl_run_pct(t: TaskDict) -> float:
    """
    "total runtime%" for a task (tree) over total lifetime

    IDEALLY would sum JUST delta CPU since last loop, BUT not (yet)
    doing incremental/delta calculations (and individual tasks may not
    be long enough lived?)
    """
    return t["_total_cpu_percent"]


def get_avg_run_pct(t: TaskDict) -> float:
    """
    get average runtime percentage for task (tree)
    """
    return get_ttl_run_pct(t) / get_ttl_tasks(t)


class FMT(NamedTuple):
    """ForMat Tuple"""

    hdr: str
    hfmt: str
    dfmt: str
    get: Callable[[TaskDict], int | float | str]


ID_FMT = FMT("Node.Id", "{:<9s}", "{:<9.9s}", get_id)
RUN_FMT = FMT("Run", "{:>6s}", "{:>6.6s}", get_runtime)
AGE_FMT = FMT("Age", "{:>6s}", "{:>6.6s}", get_age)
TASKS_FMT = FMT("Tsk", "{:>4s}", "{:>4d}", get_ttl_tasks)
TTL_PCT_FMT = FMT("Total%", "{:>6s}", "{:>6.1f}", get_ttl_run_pct)
AVG_PCT_FMT = FMT("Avg%", "{:>5s}", "{:>5.1f}", get_avg_run_pct)
DESCR_FMT = FMT("Description", "{}", "{}", lambda t: t["_descr"])


def format_rows(
    rows: list[TaskDict], cols: list[FMT], sort_key: str | None
) -> list[str]:
    if sort_key:
        # sort in place by age or runtime, highest first
        rows.sort(key=lambda x: x[sort_key], reverse=True)  # type: ignore[literal-required]

    # format header row: maybe center name in column?
    output = ["", " ".join([col.hfmt.format(col.hdr) for col in cols])]
    for row in rows:
        output.append(" ".join([col.dfmt.format(col.get(row)) for col in cols]))
    return output


################


class Parser:
    def __init__(self, s: str):
        self.s = self.orig = s

    def peek(self, t: str) -> bool:
        return self.s.startswith(t)

    def token(self, t: str) -> bool:
        if self.peek(t):
            self.s = self.s[len(t) :]
            return True
        return False

    def json(self) -> tuple[JSON, str]:
        if self.s[0] != "{":
            raise ValueError("not an object")
        j = json.JSONDecoder()
        obj, end = j.raw_decode(self.s)
        doc = self.s[:end]
        self.s = self.s[end:]
        return obj, doc

    def upto(self, t: str) -> str:
        """
        wanted to call it "break"
        """
        if t not in self.s:
            raise ValueError(f"{t} not found")
        ret, self.s = self.s.split(t, 1)
        return ret


# made a tuple so adding an argument doesn't break subclasses
class SearchRequest(NamedTuple):  # format_search_request arg
    dsl_text: str  # DSL text
    dsl_json: JSON  # parsed DSL
    indicies: str  # comma separated
    search_type: str
    routing: str
    preference: str  # session id or user


class ESQueryGetter(ESTaskGetter):
    """
    ESTaskGetter with methods to decode task descriptions
    and extract documents and queries
    """

    def format_index_request(self, j: JSON, doc: str, index: str, _id: str) -> str:
        """
        override with local formatting!
        """
        return doc

    def format_search_request(self, sr: SearchRequest) -> str:
        """
        override with local formatting!
        """
        return sr.dsl_text  # DSL as text

    def parse_index(self, p: Parser) -> str:
        index = _id = ""
        index = p.upto("]")
        if p.token("["):
            # XXX handle ValueError
            _id = p.upto("], source[")

        if not (p.token("_na_") or p.token("n/a")):
            # extract JSON document
            # https://github.com/elastic/elasticsearch/blob/f2b38823603125ea40b86866f306540185938ae4/server/src/main/java/org/elasticsearch/action/search/SearchRequest.java#L751
            j, doc = p.json()  # parse JSON document
            # should have trailing "]}"
            if doc and self.raw_descr:
                ret = doc
            else:
                ret = self.format_index_request(j, doc, index, _id)
        else:
            ret = p.orig
        return ret  # XXX dict?

    def parse_descr(self, descr: str) -> str:
        """
        parse task description
        """
        p = Parser(descr)
        if p.token("index {["):
            return self.parse_index(p)
        elif p.token("indices["):
            # XXX make into method, for override
            # here from
            # https://github.com/elastic/elasticsearch/blob/f2b38823603125ea40b86866f306540185938ae4/server/src/main/java/org/elasticsearch/action/search/SearchRequest.java#L751
            indicies = p.upto("]")
            search_type = routing = preference = ""

            p.token(", search_type[")  # XXX check return
            search_type = p.upto("]")
            if p.token(", scroll["):
                p.upto("]")
            p.token(", source[")  # XXX check return

            if not p.peek("]"):
                jdsl, query_dsl = p.json()
            p.token("]")
            if p.token(", "):
                if p.token("routing["):
                    routing = p.upto("]")
                    p.token(", ")
                if p.token("preference["):
                    preference = p.upto("]")
            if query_dsl:
                if self.raw_descr:
                    return query_dsl
                return self.format_search_request(
                    SearchRequest(
                        dsl_text=query_dsl,
                        dsl_json=jdsl,
                        indicies=indicies,
                        search_type=search_type,
                        routing=routing,
                        preference=preference,
                    )
                )
            # fall?
        return descr  # unparsed

    def get_descr(self, t: TaskDict) -> str:
        """
        get task description
        """

        if self.debug:
            print("-->", t["node"], t["id"])
        oid = self.get_opaque_id(t)
        if self.debug and oid:
            print("OID:", oid)
        descr = get_path(cast(JSON, t), "_full_data.task.description")
        if descr:
            descr = self.parse_descr(descr)
            if self.debug:
                print("DESCR (after):", descr)
        else:
            if self.debug and oid != type(self).__name__:
                print("T:", json.dumps(t))
            if self.show == Show.NORMAL:
                # don't show even if have opaque id
                # if it wouldn't be shown without one
                # EXCEPT always show instances of this code!
                if not t["action"].startswith("cluster:monitor/tasks"):
                    return ""

        if self.prefer_opaque_id:
            descr = oid or descr
        else:
            # this may show tasks w/ oids that
            # would not otherwise have been seen
            descr = descr or oid

        if descr:
            assert isinstance(descr, str)
            return descr

        # no full_data or description
        # lots of these with identical type/action, even w/in a node
        # (ones w/o full_data or description but w/ oid will have
        # been returned above)
        if self.show != Show.NORMAL:
            action = t.get("action")
            task_type = t.get("type")
            return f"{task_type}: {action}"

        return ""  # hide

    def get_top(self) -> list[str]:
        """
        return list of lines to display
        """

        self.get_tasks()
        self.total_times()

        final = []
        for t in self.trees:
            # allow get_descr to make final decision on what is seen
            descr = self.get_descr(t)
            if descr:
                t["_descr"] = descr
                final.append(t)

        # create list of format tupples, depending on latest settings
        cols = [ID_FMT]
        cols.append(RUN_FMT)
        if self.show_task_count:
            cols.append(TASKS_FMT)

        if self.show_age:
            cols.append(AGE_FMT)
        elif self.show_task_count:
            cols.append(AVG_PCT_FMT)
        else:
            cols.append(TTL_PCT_FMT)

        cols.append(DESCR_FMT)

        sort_on = "_total_runtime"  # or _total_elapsed

        return format_rows(final, cols, sort_on)


class Displayer:
    SCREEN = False

    def __init__(self, interval: float):
        self.interval = interval
        self._init()

    def _init(self) -> None:
        raise NotImplementedError()

    def start(self) -> None:  # start of refreah
        raise NotImplementedError()

    def line(self, lno: int, text: str) -> None:
        raise NotImplementedError()

    def done(self, blocking: bool = False) -> str:  # redisplay
        raise NotImplementedError()

    def cleanup(self) -> None:  # before exit
        raise NotImplementedError()


class CursesDisplayer(Displayer):
    SCREEN = True

    def _init(self) -> None:
        self._scr: curses.window = curses.initscr()
        self._getsize()

    def start(self) -> None:
        curses.noecho()
        try:
            curses.curs_set(0)  # hide cursor
        except curses.error:
            pass
        self._scr.clear()

    def _getsize(self) -> None:
        self._y, self._x = self._scr.getmaxyx()

    def line(self, lno: int, text: str) -> None:
        try:
            self._scr.addstr(lno, 0, text[: self._x])
        except curses.error:
            pass

    def done(self, blocking: bool = False) -> str:
        self._scr.refresh()  # display
        if self.interval > 0 and not blocking:
            curses.halfdelay(int(self.interval * 10))  # 10ths
        else:
            curses.cbreak()
        try:
            key = self._scr.getkey()
            if key != "KEY_RESIZE":
                return key
            self._getsize()
            # fall
        except curses.error:
            pass
        return ""

    def cleanup(self) -> None:
        curses.echo()
        curses.nocbreak()
        curses.endwin()


class TextDisplayer(Displayer):
    # XXX separate subclasses for termios vs msvcrt??
    # NOTE! does not support done(blocking=True)
    STDIN = 0

    def _init(self) -> None:
        self.lno = 0
        if termios and os.isatty(self.STDIN):
            if self.interval >= 1:
                self._wait = 10
            else:
                self._wait = int(self.interval * 10)

            self.saved = termios.tcgetattr(self.STDIN)
            new = self.saved.copy()
            new[LFLAG] &= ~(termios.ICANON | termios.ECHO)
            cc = new[CC]
            cc[termios.VMIN] = 0
            cc[termios.VTIME] = self._wait
            termios.tcsetattr(self.STDIN, termios.TCSADRAIN, new)

    def start(self) -> None:
        print("===")
        self.lno = 0

    def _print(self, text: str) -> None:
        print(text)
        self.lno += 1

    def line(self, lno: int, text: str) -> None:
        while lno < self.lno:
            self._print("")
        self._print(text)

    def _getkey(self) -> str:
        if termios:
            wait = int(self.interval * 10)  # VDELAY is 10ths of second
            c = b""
            while wait > 0:
                c = os.read(self.STDIN, 1)
                if c:
                    break
                wait -= self._wait
            return c.decode()
        elif msvcrt:
            # not tested
            delay = self.interval
            while delay >= 0:
                if msvcrt.kbhit():
                    return bytes(msvcrt.getch()).decode()
                time.sleep(1)
                delay -= 0.1
        else:
            time.sleep(self.interval)
        return ""

    def done(self, blocking: bool = False) -> str:
        assert not blocking
        return self._getkey()

    def cleanup(self) -> None:
        if termios:
            termios.tcsetattr(self.STDIN, termios.TCSADRAIN, self.saved)


class ESTop(ESQueryGetter):
    """
    Command line ESTaskGetter app that queries tasks and displays them.
    Can be sub-classed to override ESQueryGetter stub methods for
    local formatting.
    """

    def __init__(self) -> None:
        super().__init__()
        self.interval = DISPLAY_INTERVAL  # get from command line option
        self.get = self.get_top

    def banner(self) -> list[str]:
        lines = []
        t = time.strftime("%F %TZ", time.gmtime())

        assert self.es
        if True:
            # VERY small JSON document, includes reloc/initializing,
            # but no doc count
            ch = self.es.cluster.health()
            name = ch["cluster_name"]
            status = ch["status"]
            nodes = ch["number_of_nodes"]
            shards = ch["active_shards"]

            lines.append(
                f"{t}  {name}: status {status}, {nodes} nodes, {shards} shards"
            )

            # only in cluster health!
            # things slow down when these running:
            reloc = ch["relocating_shards"]
            init = ch["initializing_shards"]

            pending = ch["number_of_pending_tasks"]

            if reloc or init or pending:
                # XXX display highlighted
                lines.append(
                    f"{reloc} relocating, {init} initializing, "
                    f"{pending} pending tasks"
                )
        else:
            cs = self.es.cluster.stats()  # larger than cluster.health
            status = get_path(cs, "status")
            name = get_path(cs, "name")
            nodes = get_path(cs, "nodes.count.total")
            indices = get_path(cs, "indices.count")
            shards = get_path(cs, "indices.shards.total")
            segments = get_path(cs, "indices.segments.count")
            docs = get_path(cs, "indices.docs.count")
            lines.append(
                f"{t} {name}: {nodes} nodes, status {status}, {indices} indices"
            )
            lines.append(f"{shards} shards, {segments} segments, {docs} documents")

            # consider displaying if hit pct non-zero?
            # cache_hit = get_path(cs, "indices.query_cache.hit_count")
            # cache_miss = get_path(cs, "indices.query_cache.miss_count")
            # cache_pct = int(100*cache_hit/(cache_hit+cache_miss))

        if True:
            # "cat" interfaces are documented for human/kibana use only,
            # but CPU/loadavg not available elsewhere?
            # JUST IN: available in nodes.stats() under os.cpu
            nodes = []
            try:
                for node in self.es.cat.nodes(format="json").raw:
                    assert isinstance(node, dict)
                    name = node["name"].split(".")[0]
                    heap = node["heap.percent"]
                    cpu = node["cpu"]
                    m = node["master"]
                    if m != "*":
                        m = ""
                    nodes.append(f"{m}{name}@{cpu}/{heap}")
                nodes.sort()  # keep stable order
                ninfo = " ".join(nodes)
            except elasticsearch.ApiError as e:
                ninfo = str(e)
            lines.append(f"cpu%/heap%: {ninfo}")
        return lines

    def dump(self) -> None:
        print("===")
        q = self.get()  # before banner
        for line in self.banner():
            print(line)
        for line in q:
            print(line)

    @staticmethod
    def format_help(char: str, descr: str) -> str:
        return f"{char:<16s}{descr}"

    def toggle(self, opt: str) -> list[str]:
        """
        here with single character in `opt`
        from command line or keystroke

        ADD NEW ITEMS TO HELP BELOW!!!

        For now: please regard capital letters as reserved
        for "modes" (displaying things other than tasks)
        """
        if opt in SHOW_BY_VALUE:
            self.show = SHOW_BY_VALUE[opt]
        elif opt == "*":
            self.show_individuals = not self.show_individuals
        elif opt == "g":
            self.show_age = not self.show_age
        elif opt == "o":
            self.prefer_opaque_id = not self.prefer_opaque_id
        elif opt == "r":
            self.raw_descr = not self.raw_descr
        elif opt == "t":
            self.show_task_count = not self.show_task_count
        elif opt == "B":
            self.get = self.get_breakers
        elif opt == "I":
            self.get = self.get_indices
        elif opt == "T":
            self.get = self.get_top
        else:
            fh = self.format_help
            return [
                fh("h, H or ?", "Display this text"),
                "",
                fh("T", "Show Top Tasks (the default):"),
                # Show Enum values:
                fh("n", '"normal" display "no" actions, "no" persistent'),
                fh("a", 'Show "all"/"actions" (but not persistent tasks)'),
                fh("p", "Show all, including persistent tasks"),
                "",
                fh("*", "Toggle individuals instead of trees"),
                fh("g", "Toggle showing age instead of run%-age"),
                fh("o", "Toggle showing client opaque-id instead of query/data"),
                fh("r", "Toggle showing raw query/data (no interpretation)"),
                fh("t", "Toggle showing task count w/ avg%"),
                "",
                fh("B", "Show circuit Breaker trips"),
                fh("I", "Show indices"),
            ]

        return []  # no help needed

    def curses_display(self) -> None:
        self.loop(CursesDisplayer(self.interval))

    def text_loop(self) -> None:
        self.loop(TextDisplayer(self.interval))

    def loop(self, disp: Displayer) -> None:
        try:
            while True:
                disp.start()
                n = 0
                q = self.get()
                for line in self.banner():
                    disp.line(n, line)
                    n += 1
                for line in q:
                    disp.line(n, line)
                    n += 1

                key = disp.done()  # redisplay
                if key == "q":
                    sys.exit(0)
                if key and not key.isspace():  # ignore (white)space
                    help = self.toggle(key)
                    if help:
                        self._display_help(disp, help)
        except KeyboardInterrupt:
            pass  # prevent blather on ^C
        finally:
            disp.cleanup()

    def _display_help(self, disp: Displayer, help: list[str]) -> None:
        disp.start()
        n = 0
        for line in help:
            if line:
                disp.line(n, line)
                n += 1
        # skip a line
        disp.line(n + 1, self.format_help("q", "Quit"))
        disp.line(n + 2, self.format_help("SPACE", "Redisplay immediately"))
        if disp.SCREEN:
            # skip a line
            disp.line(n + 4, "Type any character to dismiss this screen")
            while not disp.done(True):  # discard one keystroke
                pass

    def usage(self, help: list[str]) -> NoReturn:
        sys.stderr.write(self.format_help("--help", "you're soaking in it\n"))
        sys.stderr.write(self.format_help("--once", "output once and quit\n"))
        sys.stderr.write(self.format_help("--loop", "loop outputting text\n"))
        sys.stderr.write("\n")
        sys.stderr.write("Single character command line options:\n")
        for line in help:
            sys.stderr.write(f"{line}\n")
        sys.exit(1)

    def process_args(self) -> str:
        hosts = os.environ.get("ESHOSTS")
        how = "curses"
        n = 1
        argc = len(sys.argv)
        while n < argc:
            arg = sys.argv[n]
            n += 1

            # add new options to usage() above!!
            if arg == "--url":
                if n == argc:
                    sys.stderr.write("--url needs argument\n")
                    sys.exit(1)
                hosts = sys.argv[n]
                n += 1
            elif arg in ("--loop", "--debug"):
                how = "loop"
                self.debug = arg == "--debug"
            elif arg == "--help":
                self.usage(self.toggle("?"))
            elif arg == "--once":
                how = "once"
            elif arg[0] == "-" and len(arg) > 1 and arg[1] != "-":
                for c in arg[1:]:
                    help = self.toggle(c)
                    if help:
                        self.usage(help)
            elif arg[0].isdigit():
                self.interval = float(arg)
                if self.interval == 0:
                    sys.stderr.write("interval must be non-zero\n")
                    sys.exit(1)
            elif arg == "--test-intervals":
                m = 1
                while True:
                    x = m * 0.000000123456789
                    print(x, format_interval(x))
                    if m > 1e15:
                        sys.exit(0)
                    m *= 10
            else:
                sys.stderr.write(f"Unknown option '{arg}'\n")
                sys.exit(1)

        if not hosts:
            sys.stderr.write("Must use --url or set ESHOSTS environment variable\n")
            sys.exit(1)
        self.set_urls(hosts)
        return how

    def main(self) -> None:
        how = self.process_args()
        # XXX do real argparse'ing?!!!

        if not sys.stdout.isatty() and how == "curses":
            sys.stderr.write("output not to a terminal\n")
            how = "loop"

        if how == "curses":
            self.curses_display()
        elif how == "once":
            self.dump()
        else:
            self.text_loop()

    def get_breakers(self) -> list[str]:
        ns = self.es.nodes.stats()
        nodes = ns["nodes"]
        ret = []
        format_str = None

        # XXX create column widths by getting max wid of all data?
        def fmt(items: list[str]) -> None:
            nonlocal format_str
            if not format_str:
                things = []
                dir = "<"
                for item in items:
                    ilen = len(item) + 1
                    things.append("{:%s%d.%ds}" % (dir, ilen, ilen))
                    dir = ">"
                format_str = " ".join(things)
            ret.append(format_str.format(*items))

        # XXX show incremental stats!
        # (need to stash in self.something, which needs to be cleared
        #  when "get" is changed)

        # sort so nodes stay in same order!
        for nodeid, nd in sorted(nodes.items()):
            if not ret:
                ret.append("")
                # first line: create header
                things = ["hostname", "port"]
                breakers = list(nd["breakers"])
                for name in breakers:
                    things.append(name[:12])
                fmt(things)
            things = [nd["name"].split(".")[0], nd["ip"].split(":")[-1]]
            for name in breakers:
                things.append(str(nd["breakers"][name]["tripped"]))
            fmt(things)
        return ret

    def get_indices(self) -> list[str]:
        j = self.es.indices.stats().raw
        # header: _shards.total
        rows = [
            "",
            "{:20.20s} {:6.6s} {:6.6s} {:>11.11s} {:>16.16s} {:6.6s} {:>6.6s}".format(
                "index", "health", "status", "documents", "pri.bytes", "shards", "segs"
            ),
        ]
        for name, data in j["indices"].items():
            rows.append(
                "{:20.20s} {:6.6s} {:6.6s} {:11d} {:16d} {:6d} {:6d}".format(
                    name,
                    data["health"],
                    data["status"],
                    get_path(data, "primaries.docs.count", 0),  # XXX scale
                    get_path(data, "total.store.size_in_bytes", 0),
                    get_path(data, "total.shard_stats.total_count", 0),
                    get_path(data, "total.segments.count", 0),
                )
            )
        return sorted(rows)


################################################################
# move to another file!!!


class MCESTop(ESTop):
    """
    ES top query display, with Media Cloud decode
    """

    def format_index_request(self, j: JSON, doc: str, index: str, _id: str) -> str:
        # XXX needs index name
        if "url" not in j:
            return doc  # XXX or ""
        url = j["url"]
        return f"importing {url} ({len(doc)} bytes)"

    def extract_query_string(self, j: JSON) -> tuple[str, str, int]:
        """
        returns query, date range, sources
        """

        # HIGHLY sensitive to query construction!!!
        # works for MC, for now!!!
        if j.get("size") == 0:
            id = get_path(j, "query.bool.filter.0.term._id.value", None)
            if id:
                return f"importer id check {id}", "", 0

        query_string = get_path(j, "query.query_string.query", None)
        if query_string:
            # here with news-search-api DSL???
            # try to extract user query_string
            # XXX extract date range, source count!!!
            # (and return? or format??)

            # try to extract MC user query_string, number of sources,
            # date range HIGHLY sensitive to query construction!!!
            # works for MC, for now!!!
            nsrc = 0  # number of sources
            if " AND ((" in query_string:
                # here with likely web-search simple query
                query_string, rest = query_string.split(" AND ((", 1)

                sources, dates = rest.rsplit("AND publication_date:", 1)

                # quick try at getting number of sources:
                # does not handle url_search_strings!!!
                nsrc = len(sources.split(" OR "))

                if query_string.startswith("(("):
                    query_string = query_string[2:]
            else:
                if "AND publication_date:" in query_string:
                    # need to be more careful here??
                    query_string, dates = query_string.rsplit(
                        "AND publication_date:", 1
                    )

            return query_string, dates, nsrc

        # ES/DSL based provider
        # XXX extract date range, source count
        qs = get_path(j, "query.bool.must.query_string.query", None)
        return qs, "", 0

    def format_search_request(self, sr: SearchRequest) -> str:
        request = sr.dsl_json
        query_str, dates, nsrcs = self.extract_query_string(request)

        if not query_str:
            query_str = sr.dsl_text

        query_str = query_str.replace("\n", " ")

        if nsrcs:
            query_str = f"{{{nsrcs}}} {query_str}"

        if dates:
            # skip brackets
            dates = dates[1:-1].replace(" TO ", ":")
            query_str = f"{dates} {query_str}"

        aggs = request.get("aggregations")
        if (
            aggs
            and aggs.get("dailycounts")
            and aggs.get("topdomains")
            and aggs.get("toplangs")
        ):
            query_str = f"OV: {query_str}"  # overview
        elif aggs and get_path(aggs, "sample.aggregations.topterms", None):
            query_str = f"TT: {query_str}"  # "top terms"
        elif request.get("size", 0) > 10:  # download?
            query_str = f"DL: {query_str}"
        elif request.get("size") != 0:  # leave importer checks alone
            query_str = f"OTHER: {query_str}"

        return query_str


if __name__ == "__main__":
    est = ESTop()
    est.main()
