#!/usr/bin/env python3

"""
Phil Budne @ Media Cloud, started Nov 7, 2024

This is a "top" program for Elasticsearch, showing the active
tasks ordered by CPU usage, in the spirit of
the Un*x "top" program (original by William LeFebvre c. 1984)
and pg_top (https://pg_top.gitlab.io/),
and also the 4.3BSD "systat" program.

Caveats:

I'm not a DB/ES person!

Developed with Python 3.10 & elasticsearch 8.12.1 library
against ES 8.15, 8.17

Currently only displays the totals returned, and not the incremental
changes since last display.

WILL display raw queries!!
"""

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
# US = "μs"      # curses wants .UTF-8 in locale! XXX check??
US = "us"
JSON = dict[str, Any]


class Show(Enum):
    """
    task display filter
    """

    NORMAL = "n"
    ALL = "a"  # tasks w/o description, but not persistent
    PERSISTENT = "p"


class How(Enum):
    LOOP = "loop"
    ONCE = "once"
    CURSES = "curses"


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
        get current user for "preference"
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


def task_id(t: TaskDict) -> str:
    """
    return short/displayable task id
    """
    # just enough info to track a long-running task
    node = t["node"][-4:]
    id = str(t["id"])[-4:]
    return f"{node}.{id}"


class Col:
    def __init__(
        self,
        head: str,
        wid: int,
        type_: str,
        getter: Callable[[Any], int | float | str],
        align: str = "",
    ):
        if wid == 0:
            self.col_format = f"{{:{type_}}}"
        elif type_.endswith(("d", "f")):
            if not align:
                align = ">"  # for header
            self.col_format = f"{{:{align}{wid}{type_}}}"
        else:
            self.col_format = f"{{:{align}{wid}.{wid}{type_}}}"  # str
        if wid:
            self.head = f"{{:{align}{wid}.{wid}s}}".format(head)
        else:
            self.head = head
        self.getter = getter

    def _format_col(self, arg: Any) -> str:
        return self.col_format.format(self.getter(arg))

    def __repr__(self) -> str:
        return f"<Col: {self.head.strip()}>"

    @staticmethod
    def header(cols: list["Col"]) -> str:
        return " ".join(col.head for col in cols)

    @staticmethod
    def format_row(cols: list["Col"], row: Any) -> str:
        return " ".join(col._format_col(row) for col in cols)


# Col objects for Task display (included columns vary at run time)
ID_COL = Col("Node.Id", 9, "s", task_id)
RUN_COL = Col("Run", 6, "s", lambda t: format_interval(t["_total_runtime"]), align=">")
AGE_COL = Col("Age", 6, "s", lambda t: format_interval(t["_max_age"]), align=">")
TASKS_COL = Col("Tsk", 4, "d", lambda t: t["_total_tasks"])
TTL_PCT_COL = Col("Total%", 6, ".1f", lambda t: t["_total_cpu_percent"])
AVG_PCT_COL = Col(
    "Avg%", 5, ".1f", lambda t: t["_total_cpu_percent"] / t["_total_tasks"]
)
DESCR_COL = Col("Description", 0, "s", lambda t: t["_descr"])


################


class Parser:
    """
    helper for parsing formatted strings
    """

    def __init__(self, s: str):
        self.s = self.orig = s

    def peek(self, t: str | tuple[str, ...]) -> bool:
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

    def _parse_index(self, p: Parser) -> str:
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

    def _parse_indices(self, p: Parser) -> str:
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
        return p.orig

    def _parse_reindex(self, p: Parser, task: JSON) -> str:
        """
        reindex from [host=HHH port=PPP query={JSON}][SRCINDEX] to [DESTINDEX]
        """
        # ... index/reindex/ReindexRequest.java toString:
        from_host = pct = ""
        if p.peek(("[scheme=", "[host=")):
            p.token("[")
            # index/reindex/RemoteInfo.java toString:
            host = ""
            if p.token("scheme="):
                p.upto(" ")  # returns http or https
            if p.token("host="):  # always
                host = truncate_hostname(p.upto(" "))
            if p.token("port="):  # always
                p.upto(" ")
            # XXX optional: " pathPrefix=...."
            if p.token("query="):  # always
                p.json()
            # XXX optional: " username=...."
            # XXX optional: " password=<<>>"
            # END RemoteInfo.toString
            p.upto("]")
            if host:
                from_host = f"{host}:"
        if p.token("["):
            from_index = p.upto("]")
        p.token(" to [")
        to_index = p.upto("]")
        if status := task.get("status", {}):
            try:
                created = status["created"]
                total = status["total"]
                pct = f" {(created*100)/total:.1f}%"
            except (KeyError, ValueError, ZeroDivisionError):
                pass
        return f"reindex {from_host}{from_index} to {to_index}{pct}"

    def parse_descr(self, descr: str, task: JSON) -> str:
        """
        parse task description
        """
        p = Parser(descr)
        if p.token("index {["):
            return self._parse_index(p)
        elif p.token("indices["):
            return self._parse_indices(p)
        elif p.token("reindex from "):
            return self._parse_reindex(p, task)
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
        task = get_path(cast(JSON, t), "_full_data.task")
        if task and (descr := task.get("description", "")):
            if not self.raw_descr:
                descr = self.parse_descr(descr, task)
                if self.debug:
                    print("DESCR (after):", descr)
        else:
            descr = ""
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
        cols = [ID_COL]
        cols.append(RUN_COL)
        if self.show_task_count:
            cols.append(TASKS_COL)

        if self.show_age:
            cols.append(AGE_COL)
        elif self.show_task_count:
            cols.append(AVG_PCT_COL)
        else:
            cols.append(TTL_PCT_COL)

        cols.append(DESCR_COL)

        sort_on = "_total_runtime"  # or _total_elapsed
        if sort_on:
            # sort in place by age or runtime, highest first
            final.sort(key=lambda x: x[sort_on], reverse=True)  # type: ignore[literal-required]

        output = [Col.header(cols)]
        for row in final:
            output.append(Col.format_row(cols, row))
        return output


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
        if lno >= self._y - 1:
            return
        if "\n" in text:  # for reindex
            text, _ = text.split("\n", 1)
        self._scr.addstr(lno, 0, text[: self._x])

    def done(self, blocking: bool = False) -> str:
        self._scr.refresh()  # display
        if self.interval > 0 and not blocking:
            total = int(self.interval * 10)  # 10ths
            while total > 0:
                this = total
                if this > 255:
                    this = 255
                curses.halfdelay(this)
                total -= this
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
    STDOUT = 1

    def _init(self) -> None:
        self.lno = 0
        if termios and os.isatty(self.STDIN) and os.isatty(self.STDOUT):
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
        else:
            self.saved = None

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
        if termios and self.saved:
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
        if termios and self.saved:
            termios.tcsetattr(self.STDIN, termios.TCSADRAIN, self.saved)


# make a method for override??
def truncate_hostname(host: str) -> str:
    return host.split(".")[0]


def node_name_truncate(node: dict[str, Any]) -> str:
    return truncate_hostname(cast(str, node["name"]))


# server/src/main/java/org/elasticsearch/cluster/node/DiscoveryNodeRole.java
NODE_ROLE_MAP = {  # largely untested
    "data": "d",
    "data_content": "s",
    "data_hot": "h",
    "data_warm": "w",
    "data_cold": "c",
    "data_frozen": "f",
    "data_ingest": "i",
    "master": "m",
    "voting_only": "v",
}


def node_role_chars(node: dict[str, Any], master: str) -> str:
    roles = ""
    for role in node["roles"]:
        ch = NODE_ROLE_MAP.get(role, "")
        if ch:
            if ch == "m" and node["_node_id"] == master:
                ch = "M"  # IS master
            roles += ch
        elif "?" not in roles:
            roles += "?"  # FIX by adding to _MAP
    return roles


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
        self.offset = 0

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
                    "*** "
                    f"{reloc} relocating, {init} initializing, "
                    f"{pending} pending tasks"
                    " ***"
                )
        else:
            # XXX move to a Cluster page?
            cs = self.es.cluster.stats().raw  # larger than cluster.health
            status = get_path(cs, "status")
            name = get_path(cs, "name")
            nodes = get_path(cs, "nodes.count.total")
            indices = get_path(cs, "indices.count")
            shards = get_path(cs, "indices.shards.total")
            segments = get_path(cs, "indices.segments.count")
            docs = get_path(cs, "indices.docs.count")
            cache_hit = get_path(cs, "indices.query_cache.hit_count")
            cache_miss = get_path(cs, "indices.query_cache.miss_count")
            cache_pct = 100 * cache_hit / (cache_hit + cache_miss)
            lines.append(
                f"{t} {name}: {nodes} nodes, status {status}, {indices} indices"
            )
            lines.append(
                f"{shards} shards, {segments} segments, {docs} docs, query cache hits: {cache_pct:.3f}%"
            )
        return lines

    def dump(self) -> None:
        print("===")
        q = self.get()  # before banner
        for line in self.banner():
            print(line)
        print("")
        for line in q:
            print(line)

    @staticmethod
    def format_help(char: str, descr: str) -> str:
        return f"{char:<16s}{descr}"

    def set_get(self, getter: Callable[[], list[str]]) -> None:
        self.get = getter
        self.offset = 0

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
            self.set_get(self.get_breakers)
        elif opt == "H":
            self.set_get(self.get_hot_threads)
        elif opt == "I":
            self.set_get(self.get_indices)
        elif opt == "N":
            self.set_get(self.get_nodes)
        elif opt == "P":
            self.set_get(self.get_pending_tasks)
        elif opt == "R":
            self.set_get(self.get_recovering_shards)
        elif opt == "T":
            self.set_get(self.get_top)
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
                fh("H", "Show Hot threads"),
                fh("I", "Show Indices"),
                fh("N", "Show Nodes"),
                fh("P", "Show Pending tasks"),
                fh("R", "Show shards in Recovery"),
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
                n += 1  # blank line
                for line in q[self.offset :]:
                    disp.line(n, line)
                    n += 1

                key = disp.done()  # redisplay
                if key == "q":
                    sys.exit(0)
                if key == "0":
                    self.offset = 0
                elif key == "\x04":  # ctrl-D
                    self.offset += 10
                elif key == "\x15":
                    self.offset -= 10
                    if self.offset < 0:
                        self.offset = 0
                elif key and not key.isspace():  # ignore (white)space
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

    def process_args(self) -> How:
        hosts = os.environ.get("ESHOSTS")
        how: How = How.CURSES
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
                how = How.LOOP
                self.debug = arg == "--debug"
            elif arg == "--help":
                self.usage(self.toggle("?"))
            elif arg == "--once":
                how = How.ONCE
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

        if not sys.stdout.isatty() and how == How.CURSES:
            sys.stderr.write("output not to a terminal\n")
            how = How.LOOP

        if how == How.CURSES:
            self.curses_display()
        elif how == How.ONCE:
            self.dump()
        elif how == How.LOOP:
            self.text_loop()
        else:
            assert False

    def get_breakers(self) -> list[str]:
        ns = self.es.nodes.stats()
        nodes = ns["nodes"]

        # get longest node name:
        name_wid = max(len(node_name_truncate(node)) for node in nodes.values())
        # create list of Cols on the fly!
        cols = [Col("node", name_wid, "s", node_name_truncate)]

        def make_col(breaker: str) -> Col:
            """
            return a Col for a breaker.
            Is function to create local "breaker" variable
            (avoid capturing loop vars in closures)
            """
            maxcol = 11
            name = breaker[:maxcol]  # capitalize? abbreviate??
            wid = max(len(name), maxcol)
            return Col(
                name, wid, "d", lambda node: node["breakers"][breaker]["tripped"]
            )

        for node in nodes.values():
            # loop for all breaker names:
            for breaker in sorted(node["breakers"].keys()):
                cols.append(make_col(breaker))
            break  # only loop for first node

        # XXX show incremental stats!
        # (need to stash in self.something, which needs to be cleared
        #  when "get" is changed)

        rows = []
        for node in nodes.values():
            rows.append(Col.format_row(cols, node))
        rows.sort()
        rows.insert(0, Col.header(cols))
        return rows

    def get_hot_threads(self) -> list[str]:
        # returns text:
        return cast(str, self.es.nodes.hot_threads()).split("\n")

    def get_indices(self) -> list[str]:
        j = self.es.indices.stats().raw
        indices = j["indices"]

        for name, data in indices.items():
            data["name"] = name  # for getter

        # max index name length:
        if indices:
            idx_wid = max(len(name) for name in indices)
        else:
            idx_wid = 0

        index_cols = [
            Col("Index", idx_wid, "s", lambda idx: idx["name"]),
            Col("Health", 6, "s", lambda idx: idx["health"]),
            Col("Status", 6, "s", lambda idx: idx["status"]),
            Col(
                "Documents",
                13,
                ",d",
                lambda idx: get_path(idx, "primaries.docs.count", 0),
            ),
            Col(
                "Bytes",
                18,
                ",d",
                lambda idx: get_path(idx, "primaries.store.size_in_bytes", 0),
            ),
            Col(
                "Shards",
                6,
                "d",
                lambda idx: get_path(idx, "primaries.shard_stats.total_count", 0),
            ),
            Col(
                "Segs",
                6,
                "d",
                lambda idx: get_path(idx, "primaries.segments.count", 0),
            ),
        ]
        rows = [Col.format_row(index_cols, idx) for idx in indices.values()]
        rows.sort()  # sort by index name
        rows.insert(0, Col.header(index_cols))
        return rows

    def get_nodes(self) -> list[str]:
        try:
            csmn_resp = self.es.cluster.transport.perform_request(
                "GET", "/_cluster/state/master_node"
            )
            master = csmn_resp.body["master_node"]  # internal id
        except Exception:
            master = None

        j = self.es.nodes.stats().raw
        nodes = j["nodes"]  # dict by internal name

        for node_id, data in nodes.items():
            data["_node_id"] = node_id

        name_wid = max(len(node_name_truncate(node)) for node in nodes.values())
        node_cols = [
            Col("Name", name_wid, "s", node_name_truncate),
            Col(
                "Uptime",
                6,
                "s",
                lambda node: format_interval(
                    get_path(node, "jvm.uptime_in_millis", 0) / 1000
                ),
                align=">",
            ),
            Col("Roles", 5, "s", lambda node: node_role_chars(node, master)),
            Col(
                "Shards",
                6,
                "d",
                lambda node: get_path(node, "indices.shard_stats.total_count", -1),
            ),
            Col(
                "Segs",
                6,
                "d",
                lambda node: get_path(node, "indices.segments.count", -1),
            ),
            Col(
                "Heap%",
                5,
                "d",
                lambda node: get_path(node, "jvm.mem.heap_used_percent", -1),
            ),
            Col("CPU%", 4, "d", lambda node: get_path(node, "os.cpu.percent", -1)),
            Col(
                "LAvg1",
                6,
                ".2f",
                lambda node: get_path(node, "os.cpu.load_average.1m", 1.23),
            ),
            Col(
                "LAvg5",
                6,
                ".2f",
                lambda node: get_path(node, "os.cpu.load_average.5m", 1.23),
            ),
            Col(
                "LAvg15",
                6,
                ".2f",
                lambda node: get_path(node, "os.cpu.load_average.15m", 1.23),
            ),
            Col("HTTP", 4, "d", lambda node: get_path(node, "http.current_open", -1)),
        ]
        rows = [Col.format_row(node_cols, node) for node in nodes.values()]
        rows.sort()  # sort by name
        rows.insert(0, Col.header(node_cols))
        return rows

    def get_pending_tasks(self) -> list[str]:
        j = self.es.cluster.pending_tasks().raw
        tasks = j["tasks"]
        pending_cols = [
            Col("Order", 6, "d", lambda task: task["insert_order"]),
            Col("Act", 3, "s", lambda task: " * " if task["executing"] else ""),
            Col("Prio", 6, "s", lambda task: task["priority"]),
            Col("Wait", 5, "s", lambda task: task["time_in_queue"], align=">"),
            Col("Source", 0, "s", lambda task: task["source"]),
        ]
        rows = [Col.header(pending_cols)]
        for task in tasks:
            rows.append(Col.format_row(pending_cols, task))
        return rows

    def get_recovering_shards(self) -> list[str]:
        active = not self.show_individuals  # with "*" show finished too
        j = self.es.indices.recovery(active_only=active).raw

        def get_from(shard: dict[str, str]) -> str:
            t = shard["type"]
            src = shard["source"]
            if t == "PEER":
                assert isinstance(src, dict)
                return truncate_hostname(src["name"])
            elif t == "SNAPSHOT":
                assert isinstance(src, dict)
                s = src["snapshot"]  # snapshot-DATE-ID
                return s.split("-")[1]  # date
            elif t == "EXISTING_STORE":
                return "-"
            return "?"

        raw = []
        for index, data in j.items():
            for shard in data["shards"]:
                idx = shard["index"]
                row = {
                    "index": index,
                    "shard": shard["id"],
                    "type": shard["type"],
                    "stage": shard["stage"],
                    "pri": shard["primary"],  # bool
                    "time": shard["total_time_in_millis"] / 1000,
                    "start": shard["start_time_in_millis"] / 1000,
                    "from": get_from(shard),
                    "to": truncate_hostname(shard["target"]["name"]),
                    "bytes": idx["size"]["percent"],  # formatted
                    "files": idx["files"]["percent"],  # formatted
                    "trlog": shard["translog"]["percent"],  # formatted
                }
                raw.append(row)

        recovery_cols = [
            Col("Index", 16, "s", lambda shard: shard["index"]),
            Col("Sh", 3, "d", lambda shard: shard["shard"]),
            Col(
                "P", 1, "s", lambda shard: "rp"[shard["pri"]]
            ),  # bools are ints my friend
            Col("Stage", 5, "s", lambda shard: shard["stage"].lower()),
            Col("Time", 5, "s", lambda shard: format_interval(shard["time"])),
            Col(
                "Type", 4, "s", lambda shard: shard["type"][:4].lower()
            ),  # PEER, SNAPSHOT
            Col("From", 10, "s", lambda shard: shard["from"]),  # snapshot yyyy.mm.dd
            Col("To", 6, "s", lambda shard: shard["to"]),
            Col("Files", 6, "s", lambda shard: shard["files"]),
            Col("Bytes", 6, "s", lambda shard: shard["bytes"]),
            Col("TrLog", 6, "s", lambda shard: shard["trlog"]),
        ]
        rows = [Col.header(recovery_cols)]
        if active:
            raw.sort(key=lambda s: s["time"], reverse=True)  # longest runtime first
        else:
            raw.sort(key=lambda s: s["start"], reverse=True)  # most recent first
        for shard in raw:
            rows.append(Col.format_row(recovery_cols, shard))
        return rows


if __name__ == "__main__":
    est = ESTop()
    est.main()
