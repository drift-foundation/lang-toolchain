# Stage 3 — fat Arc<Interface> representation boundary (plan)

> **Status: LANDED (ABI 10, DRIFTC_VERSION 0.28.0).**
> The activation bundle is live on `feature/fat-arc-interface-views`:
> `STAGE3_FAT_ARC_ACTIVE=True`, fat `{ctrl, data, vtable}` layout
> for `Arc<I>`, `ArcAsInterface` + `ArcFatGet` MIR ops with LLVM
> lowerings, per-I synthesized fat-destroy wrappers, std.log
> migration, and direct `arc<T=iface>` rejection
> (`E_ARC_OF_INTERFACE_DIRECT`).  Focused gates green:
> `test_fat_arc_interface_views.py` 9/9,
> `test_arc_rejects_interface_t.py` 3/3,
> `test_arc_intrinsic_bridge.py` 5/5 unchanged,
> `std_log_resolver_active` e2e ok.

See `docs/history.md` § 2026-04-18 for the landed description
(shape, MIR ops, LLVM lowering, synthesizer scoping, stdlib
migration, cross-package visibility, ABI bump).  The earlier
multi-slice plan body that lived here is preserved in
git history at commit `7f97ddc1` (`pre-slice-3`).
