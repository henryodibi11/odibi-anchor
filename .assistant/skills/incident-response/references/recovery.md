# Incident recovery

During an incident, prefer the smallest reversible containment or fix-forward action that
restores service without destroying diagnostic evidence. Record preconditions and an explicit
rollback trigger, validate in the narrowest safe scope, then confirm recovery through both
system signals and user-visible behavior. If rollback cannot preserve current work or the
blast radius is unknown, stop for the required approval. After stabilization, transfer
ordinary root-cause diagnosis to the debugging workflow.
