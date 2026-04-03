# Execution Review Policy

This note defines the mandatory closeout gates for implementation tasks in this
repository.

## Required Gates

Every implementation task must end with all of the following:

- self-review against the task acceptance criteria
- independent code review of the changed files
- validation or test execution appropriate to the change

## Extra Gate For Ontology-Changing Tasks

If a task changes any of the following:

- core nouns
- state machines
- command semantics

then it must also receive an ontology re-check before it can be marked complete.

The ontology review must confirm that the code does not collapse distinct kinds
of thing into one another, especially:

- live process vs persisted identity
- message routing vs bind policy
- status notification vs content delivery
- human operator control vs message-layer delivery

## Completion Rule

A task may be marked complete only after:

- the review findings are resolved or explicitly accepted
- the plan file has been updated with a concise log and edited files
- the resulting change has been committed locally

This policy applies to the implementation phase and is intended to keep plan
execution aligned with the runtime ontology.
