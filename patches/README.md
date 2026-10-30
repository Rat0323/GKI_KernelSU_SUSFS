# Baseline Context Adaptation

`susfs-6138-context.patch` patches the pinned SuSFS patch file itself.
It only changes three preimage include contexts and their hunk counts:

- `fs/exec.c`: stock uaccess/mmu_context follows coredump, not the newer
  dma-buf/genksyms block.
- `fs/namespace.c`: stock has no trace/hooks/blk.h at this location.
- `fs/proc/base.c`: stock has no dma-buf include at this location.

No stock C code, SuSFS added/removed C code, or sucompat hooks are changed.
Both the adaptation and the resulting filesystem patch use `git apply
--check` before application. There is no fuzzy fallback or rejected-hunk
suppression. The original unadapted patch fails on ACK common 0c3d559bcd85.
