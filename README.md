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

<img WIDTH=877 HEIGHT=636 src="https://www.regressive.org/phil/mc/es-tools/top.png">

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

For each of the images below, clicking on them will send you to an
interactive browser in the SVG file: Clicking on an element will zoom
you in on that subtree.  Click on a pastelled-out element at the
bottom, or "zoom out" in the top left to zoom out.  Typing CTRL-F
allows you to search for an element by name.

<p>
totals (without node, index, shard)
<br>
<a href="https://www.regressive.org/phil/mc/es-tools/detail-none.svg">
<img WIDTH=600 HEIGHT=83 src="https://www.regressive.org/phil/mc/es-tools/detail-none.png">
</a>
<p>
breakdown by index:
<br>
<a href="https://www.regressive.org/phil/mc/es-tools/detail-i.svg">
<img WIDTH=600 HEIGHT=91 src="https://www.regressive.org/phil/mc/es-tools/detail-i.png">
</a>
<p>
breakdown by node:
<br>
<a href="https://www.regressive.org/phil/mc/es-tools/detail-n.svg">
<img WIDTH=600 HEIGHT=91 src="https://www.regressive.org/phil/mc/es-tools/detail-n.png">
</a>
<p>
breakdown by node and index:
<br>
<a href="https://www.regressive.org/phil/mc/es-tools/detail-ni.svg">
<img WIDTH=600 HEIGHT=99 src="https://www.regressive.org/phil/mc/es-tools/detail-ni.png">
</a>
<p>
breakdown by index and shard:
<br>
<a href="https://www.regressive.org/phil/mc/es-tools/detail-is.svg">
<img WIDTH=600 HEIGHT=99 src="https://www.regressive.org/phil/mc/es-tools/detail-is.png">
</a>
<p>
breakdown by node and index and shard:
<br>
<a href="https://www.regressive.org/phil/mc/es-tools/detail-nis.svg">
<img WIDTH=600 HEIGHT=107 src="https://www.regressive.org/phil/mc/es-tools/detail-nis.png">
</a>

## Before submitting a Pull Request

Files in this repository were checked in under pre-commit checks with
all kinds of poking and prodding.  On Unix-ish systems "make install"
should install a pre-commit environment, and "make lint" should run
all checks.
