from .cli import main

if __name__ == "__main__":  # the guard matters: spawned worker processes re-import this module
    raise SystemExit(main())
