"""Deep-dive tester for the RC522 scanners. Uses the same switching as badges.py.

  cd ~/Piproject && source nfc/bin/activate
  python testscanners.py                    # all scanners, runs until Ctrl+C
  python testscanners.py --only 1,2         # just scanners 1 and 2
  python testscanners.py --seconds 30       # stop by itself and print the verdicts

What it does:
  1. Warns if another badges.py / testscanners.py is running (two copies fight over the RST pins).
  2. Isolation test: with every scanner held in reset nothing should answer. If something does,
     one scanner's RST pin isn't really under control (loose/floating wire), which can garble
     every other scanner's reads.
  3. Wake-alone test: each scanner by itself must report a healthy version register.
  4. Live loop: every few seconds prints, per scanner, the version register, whether the antenna
     is on, how each tag request went (read / miss / id-failed), how many passes read a tag,
     and which tag IDs were seen. Ctrl+C prints a plain-English verdict per scanner.

  python testscanners.py --find-leak        # if the isolation test fails: needs ONE tag in hand.
                                            # Tells you which module's RST wire isn't working.
"""
import argparse
import os
import shlex
import subprocess
import time

import RPi.GPIO as GPIO

import badges as poc

SILENT = (0x00, 0xFF)  # what an unanswered SPI read looks like


def stamp():
    return time.strftime("%H:%M:%S")


def other_copies_running():
    """Other python processes running badges.py or testscanners.py."""
    out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines()[1:]:
        pid, _, args = line.strip().partition(" ")
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            tokens = shlex.split(args)
        except ValueError:
            continue
        if len(tokens) >= 2 and os.path.basename(tokens[0]).startswith("python"):
            if os.path.basename(tokens[1]) in ("badges.py", "testscanners.py"):
                found.append(f"pid {pid}: {args}")
    return found


def isolation_test(reader):
    for s in poc.SCANNERS:
        GPIO.output(s["rst"], GPIO.LOW)
    time.sleep(0.1)
    v = poc.version(reader)
    if v in SILENT:
        print(f"  OK   all scanners in reset -> nothing answers ({hex(v)})")
        return True
    print(f"  FAIL all scanners in reset but something still answers ({hex(v)}).")
    print("       A scanner's RST wire isn't holding it in reset (loose, floating or on the wrong pin).")
    return False


def wake_alone_test(reader, scanners):
    all_ok = True
    for s in scanners:
        poc.select(reader, s)
        versions = [poc.version(reader) for _ in range(5)]
        healthy = all(v not in SILENT for v in versions) and len(set(versions)) == 1
        all_ok &= healthy
        shown = ", ".join(hex(v) for v in versions)
        print(f"  {'OK  ' if healthy else 'FAIL'} {s['name']} (RST pin {s['rst']}) alone -> {shown}")
    return all_ok


def find_leak(reader, seconds_each=5):
    """With every scanner held in reset, find which module still answers.

    Whatever is awake gets initialised, then you hold a tag on each module in turn. The
    module that reads a tag while every RST pin is low is the one whose RST isn't working.
    """
    for s in poc.SCANNERS:
        GPIO.output(s["rst"], GPIO.LOW)
    time.sleep(0.1)
    v = poc.version(reader)
    if v in SILENT:
        print("  No leak: with every scanner in reset nothing answers.")
        return
    print(f"  Something answers with every scanner in reset ({hex(v)}). Hunting for it.")
    print("  Hold ONE tag on the named scanner when told and keep it there until the next prompt.\n")

    reader.MFRC522_Init()  # only the leaking chip is awake, so only it gets initialised
    time.sleep(poc.FIELD_SECONDS)
    hits = {}
    for s in poc.SCANNERS:
        print(f"  >>> Move the tag onto {s['name']} now ... ", end="", flush=True)
        time.sleep(2)  # time to move the tag
        reads, deadline = 0, time.time() + seconds_each
        while time.time() < deadline:
            if poc.read_uid(reader):
                reads += 1
        hits[s["name"]] = reads
        print(f"{reads} reads")

    print()
    leaking = [name for name, n in hits.items() if n]
    if len(leaking) == 1:
        print(f"  FOUND: {leaking[0]} reads a tag while every RST pin is low.")
        print(f"  Its RST wire isn't holding it in reset. Check it goes to the right Pi pin and isn't tied to 3.3V or another signal.")
    elif leaking:
        print(f"  Several modules read while in reset: {', '.join(leaking)}. Their RST wires all need checking.")
    else:
        print("  No module read the tag. Try again with the tag closer, or check that a tag was actually held on each one.")


def new_stats():
    return {"passes": 0, "bad_version": 0, "antenna_off": 0, "ok": 0, "miss": 0, "idfail": 0,
            "reads": 0, "ids": set(), "version": 0}


def poll(reader, scanner, stats_list):
    """One pass on one scanner, recording everything into each dict in stats_list."""
    poc.select(reader, scanner)
    version = poc.version(reader)
    antenna_on = (reader.Read_MFRC522(reader.TxControlReg) & 0x03) == 0x03

    uid = None
    outcomes = []
    for _ in range(poc.READ_TRIES):
        # The library reports "no tag" as either MI_NOTAGERR or MI_ERR, so both count as a miss.
        status, _ = reader.MFRC522_Request(reader.PICC_REQIDL)
        if status == reader.MI_OK:
            st, raw = reader.MFRC522_Anticoll()
            if st == reader.MI_OK:
                outcomes.append("ok")
                uid = 0
                for byte in raw[:5]:
                    uid = uid * 256 + byte
                break
            outcomes.append("idfail")  # a tag answered but its ID couldn't be read
        else:
            outcomes.append("miss")
        time.sleep(0.01)

    for st in stats_list:
        st["passes"] += 1
        st["version"] = version
        st["bad_version"] += version in SILENT
        st["antenna_off"] += not antenna_on
        for o in outcomes:
            st[o] += 1
        if uid:
            st["reads"] += 1
            st["ids"].add(uid)


def label_ids(ids):
    if not ids:
        return "-"
    parts = []
    for uid in sorted(ids):
        owner = poc.owner_of(uid)
        parts.append(f"{uid} ({owner}'s tag)" if owner else f"{uid} (unknown tag)")
    return ", ".join(parts)


def verdict(st):
    if st["passes"] == 0:
        return "never polled"
    if st["bad_version"]:
        return "NOT RESPONDING on some/all passes - check its power, SCK/MOSI/MISO/SDA and RST wires"
    if st["antenna_off"]:
        return "chip answers but its antenna was off - re-init problem, tell Claude"
    if st["reads"] == 0:
        if st["idfail"]:
            return "a tag answers but its ID never reads - tag too far, interference, or bus contention"
        return "no tag detected - is a tag right on it? (the chip itself is healthy)"
    if st["reads"] < st["passes"]:
        return f"FLAKY - read a tag on {st['reads']}/{st['passes']} passes (tag distance / power?)"
    return f"good - read a tag on every pass ({st['reads']}/{st['passes']})"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="comma-separated scanner numbers to test, e.g. 1,2")
    parser.add_argument("--seconds", type=float, help="stop after this many seconds")
    parser.add_argument("--window", type=float, default=5, help="seconds between live reports")
    parser.add_argument("--find-leak", action="store_true",
                        help="find which module keeps answering while every RST pin is low (needs one tag)")
    args = parser.parse_args()

    scanners = poc.SCANNERS
    if args.only:
        wanted = {int(n) for n in args.only.split(",")}
        scanners = [s for i, s in enumerate(poc.SCANNERS, 1) if i in wanted]
        if not scanners:
            raise SystemExit(f"--only {args.only} matches no scanner (1-{len(poc.SCANNERS)})")

    print("== Checking for other copies ==")
    copies = other_copies_running()
    if copies:
        for c in copies:
            print(f"  WARNING {c}")
        print("  Another copy fights over the RST pins and will corrupt these results. Stop it first (Ctrl+C in its terminal).")
    else:
        print("  OK   nothing else is running")

    reader = poc.setup()

    if args.find_leak:
        print("\n== Leak hunt ==")
        find_leak(reader)
        return

    print("\n== Isolation test ==")
    isolated = isolation_test(reader)
    print("\n== Wake-alone test ==")
    wake_alone_test(reader, scanners)

    print(f"\n== Live: {', '.join(s['name'] for s in scanners)} - hold tags to them (Ctrl+C for verdicts) ==")
    total = {s["name"]: new_stats() for s in scanners}
    window = {s["name"]: new_stats() for s in scanners}
    started = window_start = time.time()

    try:
        while True:
            for s in scanners:
                poll(reader, s, [total[s["name"]], window[s["name"]]])

            now = time.time()
            if now - window_start >= args.window:
                print(f"\n[{stamp()}] last {now - window_start:.0f}s")
                for s in scanners:
                    w = window[s["name"]]
                    ant = "off!" if w["antenna_off"] else "on"
                    print(f"  {s['name']} (RST {s['rst']}): version {hex(w['version'])}, antenna {ant} | "
                          f"requests: read {w['ok']} / miss {w['miss']} / id-failed {w['idfail']} | "
                          f"tag read {w['reads']}/{w['passes']} passes | IDs: {label_ids(w['ids'])}")
                    window[s["name"]] = new_stats()
                window_start = now

            if args.seconds and now - started >= args.seconds:
                break
            time.sleep(poc.POLL_SECONDS)
    except KeyboardInterrupt:
        print()

    print("== Verdicts ==")
    if not isolated:
        print("  NOTE: the isolation test failed, so a leaking scanner may be corrupting every read below.")
        print("        Run with --find-leak to identify it before trusting these results.")
    for s in scanners:
        t = total[s["name"]]
        print(f"  {s['name']}: {verdict(t)}")
        if t["ids"]:
            print(f"      tags seen: {label_ids(t['ids'])}")


try:
    main()
finally:
    GPIO.cleanup()
