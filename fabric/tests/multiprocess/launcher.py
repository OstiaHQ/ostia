#!/usr/bin/env python3
"""Start N ranks of a multi-process test and check their transport (RFC-0001 §4.1).

    launcher.py --ranks 2 -- <binary> [args...]

Each rank gets --rank R --size N --dir <shared dir>. UCX is restricted to TCP over the
loopback device (UCX_TLS=tcp,self, UCX_NET_DEVICES=lo); rank 1's endpoint info must
show a tcp lane on lo, or the run fails even if the data arrived.
"""

import argparse
import os
import subprocess
import sys
import tempfile

LOOPBACK = "lo"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ranks", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--tls", default="tcp,self", help="UCX_TLS")
    parser.add_argument(
        "--expect", default="tcp/" + LOOPBACK, help="lane rank 1's endpoint must use"
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    env = dict(os.environ, UCX_TLS=args.tls, UCX_NET_DEVICES=LOOPBACK)
    print(f"UCX_TLS={env['UCX_TLS']} UCX_NET_DEVICES={env['UCX_NET_DEVICES']}", flush=True)
    with tempfile.TemporaryDirectory(prefix="ostia-mp-") as rendezvous:
        procs = [
            subprocess.Popen(
                [*command, "--rank", str(r), "--size", str(args.ranks), "--dir", rendezvous],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for r in range(args.ranks)
        ]
        outputs, codes = [], []
        for r, p in enumerate(procs):
            try:
                out, _ = p.communicate(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                for q in procs:
                    q.kill()
                print(f"error: rank {r} timed out after {args.timeout} s", file=sys.stderr)
                return 1
            outputs.append(out)
            codes.append(p.returncode)
            print(f"--- rank {r} (exit {p.returncode})\n{out}", flush=True)
    if all(code == 77 for code in codes):
        return 77  # ctest SKIP_RETURN_CODE: the ranks found no CUDA device
    if any(codes):
        return 1
    evidence = outputs[1] if len(outputs) > 1 else ""
    if args.expect not in evidence:
        print(
            f"error: no {args.expect} lane in rank 1's endpoint info\n"
            f"  rule: the multi-process harness runs over UCX TCP loopback\n"
            f"  fix: check that UCX has the tcp transport (ucx_info -d) and a '{LOOPBACK}' device\n"
            "  see: RFC-0001 §4.1",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
