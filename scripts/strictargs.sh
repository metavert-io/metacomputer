# scripts/strictargs.sh: the command-line rules every metacomputer bash program follows (lib/strictargs.py is the same
# for its Python ones). Sourced first, before anything else runs; it only defines functions and reads nothing.
#
#   sa_check PROG "USAGE" MIN MAX "FLAGS" "VALUE-OPTIONS" ARGS...
#
# returns 0 when ARGS are a command line of PROG, and 1 when they ask for its usage (exactly -h or --help, as a word:
# the caller prints its usage and exits 0, doing nothing else). Anything else it wouldn't use is a usage error: it says
# so on stderr and exits 2, before anything runs:
#   - something shaped like a request for usage (-hx, --help=x, --h, --he, --hel), or one given as an option's value;
#   - an option not in FLAGS or VALUE-OPTIONS (a value option written "--x=" there also takes --x=VALUE);
#   - a flag or a value option given twice;
#   - a value that starts with '-' (an option typed in the wrong place: no value of ours does);
#   - fewer than MIN or more than MAX words (MAX "n": no limit). A word starting with '-' is an option, never a word.
# MCOMP_PARSE_ONLY=1 (tests/test_help.py's valid forms): once a command line is taken, say so and exit 0, doing nothing
# else; a program that checks more after sa_check sets SA_DEFER=1 and calls sa_parsed PROG after those checks.
# SA_DASHDASH=1 lets `--` end the options (the rest are words, counted, whatever they look like); otherwise `--` is a
# usage error like any unknown option.

if [ "${BASH_SOURCE[0]}" = "$0" ]; then    # run on its own: it's sourced, and does nothing (exit 2; -h: this line)
  case "$*" in -h|--help) echo "scripts/strictargs.sh: sourced by metacomputer's bash programs (sa_check), never run"; exit 0 ;; esac
  echo "scripts/strictargs.sh: sourced by metacomputer's bash programs; nothing was run" >&2; exit 2
fi

sa_shown() { printf "'%s'" "$(printf '%s' "$1" | LC_ALL=C tr -c '[:print:]' '?' | cut -c1-60)"; }

sa_is_help_shaped() { # WORD: 0 when it's a request for usage or looks like one
  case "$1" in -h|--help|--h|--he|--hel|--help=*) return 0 ;; --*) return 1 ;; -h*) return 0 ;; esac
  return 1
}

sa_check() { # PROG USAGE MIN MAX "FLAGS" "VALUE-OPTIONS" ARGS...
  local prog="$1" usage="$2" min="$3" max="$4" flags=" $5 " vals=" $6 " seen=" " n=0 a o help=1 ends=0
  shift 6
  while [ $# -gt 0 ]; do
    a="$1"
    if [ "$ends" = 1 ]; then n=$((n + 1)); shift; continue; fi
    case "$a" in
      -h|--help) help=0; shift; continue ;;
      --) if [ "${SA_DASHDASH:-0}" = 1 ]; then ends=1; shift; continue; fi ;;
    esac
    if sa_is_help_shaped "$a"; then
      sa_fail "$prog" "$usage" "$(sa_shown "$a") isn't a request for usage: -h or --help"
    fi
    case "$a" in
      --*=*)
        o="${a%%=*}"
        case "$vals" in *" $o= "*)
          case "$seen" in *" $o "*) sa_fail "$prog" "$usage" "$o given twice" ;; esac
          sa_value_ok "$prog" "$usage" "$o" "${a#*=}"
          seen="$seen$o "; shift; continue ;;
        esac
        case "$flags" in *" $o "*) sa_fail "$prog" "$usage" "$o takes no value" ;; esac ;;
    esac
    case "$vals" in *" $a "*|*" $a= "*)
      [ $# -ge 2 ] || sa_fail "$prog" "$usage" "$a needs a value"
      case "$seen" in *" $a "*) sa_fail "$prog" "$usage" "$a given twice" ;; esac
      sa_value_ok "$prog" "$usage" "$a" "$2"
      seen="$seen$a "; shift 2; continue ;;
    esac
    case "$flags" in *" $a "*)
      case "$seen" in *" $a "*) sa_fail "$prog" "$usage" "$a given twice" ;; esac
      seen="$seen$a "; shift; continue ;;
    esac
    case "$a" in -?*) sa_fail "$prog" "$usage" "unknown option $(sa_shown "$a")" ;; esac
    n=$((n + 1))
    [ "$max" = n ] || [ "$n" -le "$max" ] || sa_fail "$prog" "$usage" "unexpected word $(sa_shown "$a")"
    shift
  done
  [ "$help" = 0 ] && return 1
  [ "$n" -ge "$min" ] || sa_fail "$prog" "$usage" "a word is missing"
  SA_FLAGS="$flags" SA_VALS="$vals" SA_WORDS="$n" SA_MAX="$max"
  [ "${SA_DEFER:-0}" = 1 ] || sa_parsed "$prog"
  return 0
}

sa_json_list() { # WORDS: a JSON list of them (option names: no quotes or backslashes in them)
  local w out="" sep=""
  for w in $1; do out="$out$sep\"${w%=}\""; sep=", "; done
  printf '[%s]' "$out"
}

sa_parsed() { # PROG: its command line is taken (MCOMP_PARSE_ONLY=1: say so, and what its grammar declares; exit 0)
  [ "${MCOMP_PARSE_ONLY:-}" = 1 ] || return 0
  printf 'parse-only: %s: taken {"flags": %s, "values": %s, "words": %s, "max_words": %s}\n' "$1" \
    "$(sa_json_list "${SA_FLAGS:-}")" "$(sa_json_list "${SA_VALS:-}")" "${SA_WORDS:-0}" \
    "$([ "${SA_MAX:-0}" = n ] && echo null || echo "${SA_MAX:-0}")"
  exit 0
}

sa_value_ok() { # PROG USAGE OPTION VALUE
  if sa_is_help_shaped "$4"; then sa_fail "$1" "$2" "$3 needs a value, not a request for usage"; fi
  case "$4" in -?*) sa_fail "$1" "$2" "$3 needs a value, not an option ($(sa_shown "$4"))" ;; esac
  return 0
}

sa_fail() { # PROG USAGE WHY
  printf '%s: %s (nothing was run)\nusage: %s\n' "$1" "$3" "$2" >&2
  exit 2
}
