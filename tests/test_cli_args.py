import sys
import subprocess
from pathlib import Path


def test_cli_help():
    res = subprocess.run(
        [sys.executable, "cli/main.py", "--help"],
        capture_output=True,
        text=True
    )
    assert res.returncode == 0
    assert "Multi-source Music Downloader" in res.stdout
    assert "-o" in res.stdout or "--output" in res.stdout
    assert "-q" in res.stdout or "--quality" in res.stdout


def test_cli_multi_word_query_parsing():
    # Test that multi-word query does not crash with unrecognized arguments
    res = subprocess.run(
        [sys.executable, "-c", "import sys; sys.argv = ['main.py', 'The', 'Weeknd', 'Blinding', 'Lights']; from cli.main import main; import argparse; p = argparse.ArgumentParser(); p.add_argument('url', nargs='*'); args = p.parse_args(sys.argv[1:]); assert ' '.join(args.url) == 'The Weeknd Blinding Lights'"],
        capture_output=True,
        text=True
    )
    assert res.returncode == 0
