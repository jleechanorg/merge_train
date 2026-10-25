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
3. Slash role is decided from the previous *token*, not its last character:
   `origin / 2`, `begin / 2`, `value++ / 2` and `{} / 2` stay division and still
   resolve a following `workdir`; a slash whose role is undecidable marks the
   whole scan unresolved rather than picking a repository.
4. A regex preceded by a block comment is still recognised as a regex, and a
   regex that never closes leaves no previously found path trusted.
5. `python3 -m pytest tests/ -q` is green.

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
decision can swallow source up to a later `/` and hide a `workdir` property, and —
worse — can leave an *earlier* property trusted where JavaScript's duplicate-key
semantics select a later one. That is wrong-repository attribution, not merely a
missed property, and it is the failure this document must state plainly.

Mitigations, in order of strength: the scanner never evaluates text; a slash whose
role is undecidable (`+`, `-`, `}`, unterminated comment) marks the whole scan
unresolved, which fails open with the existing "cannot identify checkout" path; an
unterminated regex sets the same ambiguity signal; and no input becomes *less*
ambiguous than it was before this change. `+`/`-`/`}` were removed from the
regex-preceder set precisely because the first revision guessed there and a review
finding showed it producing a wrong retained path (`origin / 2` with duplicate
`workdir` keys).

Residual, pre-existing and unchanged: a regex immediately after a block comment was
previously unrecognised and could leave a stale earlier path trusted. It is now
recognised, and the duplicate-key case therefore resolves to ambiguity.

## Known residual limits (reviewed, documented, not fixed here)

Both were reported by the independent `/dot` reviewer and are **pre-existing** — they
are not caused by this change, and each returns the same result on the base commit:

1. The previous-token table treats `//` and `/*` appearing inside a regex character
   class as the start of a comment, because the table cannot know a slash opened a
   regex without the very classification it is used for. A source that puts a comment
   marker inside a regex class and also carries duplicate `workdir` keys can therefore
   still retain an earlier path.
2. Lexical completeness for the `regex_preceders` set `( , = : [ ! & | ? ; * % < > ~ ^`
   is argued, not proven. An adversarial review found no counterexample attributable
   solely to that set, which is weaker than a proof.

Neither residual is new, and no reported case makes any input *less* safe than the
base commit: the safety property claimed here is "never worse than base, and better
for the reported inputs", not "complete JavaScript lexing".
