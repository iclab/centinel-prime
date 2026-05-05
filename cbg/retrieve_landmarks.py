#! /usr/bin/python3
# -*- coding: utf-8 -*-
# Retrieve a list of landmarks from RIPE.
# Copyright 2018, 2019 Zack Weinberg <zackw@panix.com> &
#                      Shinyoung Cho <cho.grace71@gmail.com>
#
# This program is free software:  you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.  See the file LICENSE in the
# top level of the source tree containing this file for further details,
# or consult <https://www.gnu.org/licenses/>.

"""Retrieve data on all of the publicly accessible RIPE anchors
from their web service and classify them as usable or not.
Writes a CSV file, listing all usable anchors, to standard output.

usage: $ python3 retrieve_landmarks.py > landmarks.csv
"""

import argparse
import csv
import datetime
import sys
import time
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Optional, TypeVar
from urllib.parse import urlencode, urljoin

import requests

#
# Utility
#

QUIET = False  # type: bool
START = None  # type: Optional[float]


def progress(message: str, *args: Any, **kwargs: Any) -> None:
    """Print a progress report message with elapsed time."""
    global START, QUIET
    if QUIET:
        return

    now = time.monotonic()
    if START is None:
        START = now

    sys.stderr.write(
        "{}: {}\n".format(
            datetime.timedelta(seconds=now - START), message.format(*args, **kwargs)
        )
    )


def log_exception(msg: str, exc: BaseException) -> None:
    import traceback

    msg = "*** {}: {}".format(msg, exc)
    if QUIET:
        sys.stderr.write(msg + "\n")
    else:
        progress(msg)
    traceback.print_exc(file=sys.stderr)


#
# Atlas interaction
#

T = TypeVar("T")
BASE_URL = "https://atlas.ripe.net/api/v2/"


def retrieve_atlas(
    sess: requests.Session,
    endpoint: str,
    *,
    constructor: Callable[[Mapping[str, Any]], T],
    filter: Callable[[Mapping[str, Any]], bool],
    params: Mapping[str, str] = {}
) -> Iterable[T]:

    query_url = urljoin(BASE_URL, endpoint)
    if query_url[-1] != "/":
        query_url += "/"
    if params:
        query_url = urljoin(query_url, "?" + urlencode(params))

    retries = 0
    page = 1
    while True:
        progress("retrieve_atlas: getting page {}".format(page))
        try:
            resp = sess.get(query_url)
            resp.raise_for_status()
            blob = resp.json()
        except Exception as e:
            retries += 1
            if isinstance(e, requests.exceptions.ChunkedEncodingError):
                progress(
                    "retrieve_atlas: {}: protocol error{}".format(
                        endpoint, ", retrying" if retries < 5 else ""
                    )
                )
            else:
                log_exception(
                    "retrieve_atlas [endpoint={} params={!r}]".format(endpoint, params),
                    e,
                )
            if retries >= 5:
                break
            time.sleep(5)
            continue

        if isinstance(blob, list):
            next_url = None
        else:
            next_url = blob.get("next")
            blob = blob["results"]
        for obj in blob:
            if filter(obj):
                yield constructor(obj)

        if next_url is None:
            break
        query_url = urljoin(query_url, next_url)
        page += 1


#
# Probe and anchor lists
#

Landmark = NamedTuple(
    "Landmark",
    [
        ("addr", str),
        ("aid", int),
        ("pid", int),
        ("longitude", float),
        ("latitude", float),
        ("city", str),
        ("country", str),
        ("asn", int),
    ],
)


def landmark_from_anchor_json(blob: Mapping[str, Any]) -> Landmark:
    assert blob["geometry"]["type"] == "Point"
    return Landmark(
        addr=blob["ip_v4"],
        pid=blob["probe"]["id"],
        aid=blob["id"],
        longitude=blob["geometry"]["coordinates"][0],
        latitude=blob["geometry"]["coordinates"][1],
        city=blob["city"],
        country=blob["country"],
        asn=blob["as_v4"],
    )


def anchor_is_usable(blob: Mapping[str, Any]) -> bool:
    return (
        blob.get("ip_v4") is not None
        and blob["probe"]["status"]["name"] == "Connected"
        # TODO: Why is it necessary to apply a filter for cases
        # where the latitude is below -60 or above 85?
        # and -60 <= blob["geometry"]["coordinates"][1] <= 85
    )


def retrieve_active_anchor_list() -> Iterable[Landmark]:
    progress("retrieving active anchor list...")

    session = requests.Session()
    session.headers["User-Agent"] = (
        "inter-anchor-rtt-retriever-1.0; zackw at cmu dot edu"
    )

    return retrieve_atlas(
        session,
        "anchors",
        params={"include": "probe"},
        constructor=landmark_from_anchor_json,
        filter=anchor_is_usable,
    )


def write_active_anchor_list(anchors: Iterable[Landmark]) -> None:
    with sys.stdout as ofp:
        wr = csv.writer(ofp, dialect="unix", quoting=csv.QUOTE_MINIMAL)
        wr.writerow(Landmark._fields)
        for lm in anchors:
            wr.writerow(lm)
  

#
# Master control
#


def main() -> None:
    global QUIET

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "-q", "--quiet", action="store_true", help="Don't print progress messages."
    )
    args = ap.parse_args()

    QUIET = args.quiet

    write_active_anchor_list(retrieve_active_anchor_list())
    progress("done")


if __name__ == "__main__":
    main()
