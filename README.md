# Elasticsearch Tools

Elasticsearch Tools developed at Media Cloud

* Not (yet) packaged
* May (or may not) be submitted to PyPI

## es_top.py

A "top" program for Elasticsearch, showing the active tasks ordered by
CPU usage, in the spirit of the `Un*x` "top" program (original by
William LeFebvre c. 1984) and pg_top (https://pg_top.gitlab.io/), and
also the 4.3BSD "systat" program.

Requires "elasticsearch" Python package.

`ESTop` class can be subclassed to do local interpretation of data and
queries (see below).

Initial work by Phil Budne, funded by an NSF grant.

## mc-es-top.py

Media Cloud customized version of es_top
(see above).

## collapse-esperf.py

Reads Elasticsearch JSON `"profile"` data and collapses profiling tree
into input for https://github.com/brendangregg/FlameGraph/blob/master/flamegraph.pl
a tool for the graphical exploration of profiling data
(load the resulting .svg file into a browser).

Initial work by Phil Budne, funded by an NSF grant.

## Before submitting a Pull Request

Files in this repository were checked in under pre-commit checks with
all kinds of poking and prodding.  On Unix-ish systems "make install"
should install a pre-commit environment, and "make lint" should run
all checks.
