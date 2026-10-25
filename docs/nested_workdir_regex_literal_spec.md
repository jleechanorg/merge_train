# Nested workdir resolution across JS regex literals (issue #55)

Specification for the change that makes `_scan_literal_exec_command_workdirs` read a
`workdir:` property written after a JavaScript regular-expression literal.

## Outcome

A nested `tools.exec_command({...})` call whose value contains a regex literal is
resolved to its own `workdir`, so the conflict check inspects the checkout the edit
actually lands in instead of falling back to the hook's outer cwd.

## Non-goals

- No general-purpose JavaScript parser. The scanner stays a conservative literal
  reader that refuses to evaluate expressions.
- No change to which inputs are treated as ambiguous. Dynamic, shorthand, computed,
  spread, duplicate and non-object `workdir` forms keep their current unresolved
  behaviour.
- No new dependency, no network call, no change to the hook's output schema,
  enforcement policy, cache, or timeout.

## Acceptance

1. For each of these sources, `_scan_literal_exec_command_workdirs` returns
   `(True, False, [<repo>])` and `_mutation_context_directories` returns `[<repo>]`:
   - a character class containing quote characters: `.replace(/['"]/g, "")`
   - an escaped slash inside a class: `.split(/[\/]/)[0]`
   - escape sequences before the property: `.replace(/\s+/g, " ")`
2. Every pre-existing scanner test keeps passing unchanged — in particular the
   ambiguity cases (getter, shorthand, computed key, spread, duplicate key,
   template interpolation, named options object) still yield no context.
3. Division expressions are not mistaken for regex literals: `a / b / c` before a
   `workdir` property still resolves that property.
4. `python3 -m pytest tests/ -q` is green.

## User stories

- As an agent editing a file from inside a nested command that formats its output
  with a regex, my edit is checked against that command's directory, so an
  overlapping open PR is reported instead of missed.
- As an operator, a wrong-checkout conflict warning stops appearing for edits that
  actually landed in a different repository.

## Proof plan

- RED: the new regression test fails before the fix with `[None]` instead of the
  workdir path, captured as a log artifact.
- GREEN: the same test passes after the fix; the full suite runs on the same SHA.
- Behavioural: the shipped wrapper run end-to-end against two git repositories and a
  fake `gh`, showing deny on overlap and silence on no-overlap.
- Claim map: each acceptance item is tied to a named test or command in the PR
  evidence index; original tested SHA recorded there.

## Mock

`gh` is a five-line shell stub in the end-to-end proof. It stands in only for the
GitHub CLI's stdout and exit status; the hook's own parsing, resolution, cache,
enforcement lookup and decision payload all run for real against real git
repositories. No mock replaces a boundary this change touches. Exemption instead of
live GitHub calls is justified because the claim under test is path resolution, and
a live call cannot make an arbitrary local test repository have overlapping PRs.

## Risk

The scanner now decides whether `/` opens a regex from the previous token. A wrong
decision could swallow source up to the next `/` and hide a `workdir` property. The
blast radius is bounded: the scanner never evaluates text, an unrecognised slash
keeps the previous opaque treatment, and any resulting ambiguity still fails open
with the existing "cannot identify checkout" path rather than denying. Mitigation is
the division-expression acceptance case plus the unchanged ambiguity tests.
