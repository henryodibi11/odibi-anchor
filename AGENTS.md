# Repository guidance

Read and follow the canonical `.assistant_instructions.md` contract before substantive work. Preserve the public package identity: distribution `odibi-anchor`, import `odibi_anchor`, and CLI `anchor`.

Keep runtime state outside the repository and distribution artifacts. Do not commit databases, sessions, generated manifests, environment files, credentials, or private project content. Run focused tests and packaging checks for every change; never claim checks that were not executed.
