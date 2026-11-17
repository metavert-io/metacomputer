// mcomp-vault: entry point. See Commands.swift for the commands and SECURITY.md
// ("Secrets and logins") for what it protects and what it doesn't.
import Darwin

exit(runMain(Array(CommandLine.arguments.dropFirst())))
