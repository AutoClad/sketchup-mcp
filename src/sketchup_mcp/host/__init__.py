"""What the pool needs from the system it runs on.

launcher.py holds the pool's rules, which are the same everywhere. The
parts that differ by system are in one module per system, each with the
same names:

    DATA_DIR                      where the pool's folder goes
    SKETCHUP_EXE                  SketchUp's program, unless SKETCHUP_EXE is set
    START_LOG_HINT                where to look when SketchUp exits while starting
    KILLED_WITH                   what kill uses, for a message

    process_started(pid)          when a running process started, or None
    processes()                   {pid: (parent pid, program name)}
    image_path(pid)               a running process's program, in full
    PYTHON_LAUNCHERS              programs that only start Python for another
    CLI_WRAPPERS                  the same, for the sketchup-launcher CLI
    is_desktop_app(path)          whether a program is the Claude desktop app

    lock(f) / unlock(f)           the pool's lock file; lock raises OSError if held
    make_shared_dir(path)         create the pool's folder where every process sees it

    start(r, src, exe, template, pool_dir, pool_file)
                                  start SketchUp for instance r; (pid, start time)
    spawn(request, pool_file)     Windows only: what its scheduled task runs
    kill(pid)                     end a SketchUp
"""


class PoolError(Exception):
    """A lease or release that can't be done, with why."""
