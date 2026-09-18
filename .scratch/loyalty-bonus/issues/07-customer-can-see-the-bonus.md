# 07: The customer can see the bonus

**What to build:** The dashboard shows what each deposit will pay and when, and vested
bonuses are distinguishable from base points in history. The savings-activity table already
shows a "Next anniversary" column per lot; it gains the projected bonus for the **surviving**
portion beside it. History labels vested entries so a customer can tell a bonus from base
points earned on a deposit.

Every value reads straight from the seam — the UI holds no rule of its own (D45), and the
frontend never touches the database.

**Blocked by:** 03 (A partially withdrawn lot vests on its surviving portion), so the number
on screen is the surviving-portion number rather than one that changes later; and 04
(Forfeiture is recorded as an explicit fact) if forfeitures are surfaced in history.

**Status:** ready-for-agent

- [ ] Anke's dashboard shows a projected bonus of 110 against her 20 Jun lot (example C).
- [ ] Vested entries are distinguishable from base earning in the history view.
- [ ] No bonus arithmetic lives in the frontend — the projection and the labels come from
      the API (D45).
- [ ] Verified by driving the running app end to end, not by asserting on components or UI
      state (T9), using the existing browser smoke harness documented in
      `project_starter/readme.md` rather than an improvised one.
- [ ] `ERR_ABORTED` from React StrictMode double-invoked effects stays expected and is not
      surfaced to the user as an error.
- [ ] `npm run lint` and `npm run build` pass in `app/frontend`; `pytest` and
      `ruff check .` pass in `app/backend`.
