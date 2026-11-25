"""Claude Code's sessions for `metacomputer learn` (lib/transcripts.py reads them: roots). Two Claude Code formats carry
the same messages, which lib/transcripts.py's parse() reads itself (they are also the format bench turns every agent's
events into): a headless session's stream-json output, and Claude Code's own session files,
$CLAUDE_CONFIG_DIR/projects/<project>/<session>.jsonl (else ~/.claude/projects), subagent files below a session's
folder included: a Transcript of kind "project" (its default)."""
import os

KIND = "project"                            # its own session files' Transcript kind


def claude_projects_dir(env=None):
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def roots(env):
    """The folders lib/learn_mine.py walks for its session files."""
    return [claude_projects_dir(env)]
