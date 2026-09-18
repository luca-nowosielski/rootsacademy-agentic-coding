import { useCallback, useEffect, useRef, useState } from 'react'
import { apiGet, apiPost, newIdempotencyKey } from './api.js'
import Login, { Brand } from './Login.jsx'

const TABS = [
  ['overview', 'Overview'],
  ['money', 'Move money'],
  ['rewards', 'Rewards'],
  ['history', 'History'],
]

// The seam names the reason; this only makes it readable. An unknown reason
// falls through to whatever the seam called it, so a later ticket adding one
// can never leave a blank cell here.
const REASON_LABELS = {
  deposit: 'Deposit',
  deposit_reversal: 'Deposit reversed',
  claim: 'Claimed',
  expiry: 'Expired',
  vesting: 'Loyalty bonus',
}

const money = value => new Intl.NumberFormat('en-IE', { style: 'currency', currency: 'EUR' }).format(value)

function formatDay(iso) {
  return new Date(iso).toLocaleDateString('en-GB', {
    timeZone: 'Europe/Brussels',
    dateStyle: 'medium',
  })
}

function formatMoment(iso) {
  return new Date(iso).toLocaleString('en-GB', {
    timeZone: 'Europe/Brussels',
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function Field({ label, ...input }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      <input {...input} />
    </label>
  )
}

function ClaimConfirmation({ item, busy, onConfirm, onCancel }) {
  const dialog = useRef(null)
  useEffect(() => {
    const element = dialog.current
    const previousFocus = document.activeElement
    element.showModal()
    return () => {
      element.close()
      previousFocus?.focus()
    }
  }, [])
  return (
    <dialog ref={dialog} className="confirm-dialog" aria-labelledby="claim-title"
      aria-describedby="claim-warning" onCancel={(event) => {
        event.preventDefault()
        if (!busy) onCancel()
      }}>
      <h2 id="claim-title">Claim {item.name}?</h2>
      <p><strong>{item.price_points} points</strong></p>
      <p id="claim-warning" className="warning">Your voucher is issued immediately. Points cannot be refunded.</p>
      <div className="dialog-actions">
        <button type="button" className="secondary" onClick={onCancel} disabled={busy} autoFocus>Cancel</button>
        <button type="button" onClick={onConfirm} disabled={busy}>{busy ? 'Claiming…' : 'Confirm claim'}</button>
      </div>
    </dialog>
  )
}

const SESSION_KEY = 'saving-streak-starter-email'
function rememberEmail(email) {
  try {
    if (email) sessionStorage.setItem(SESSION_KEY, email)
    else sessionStorage.removeItem(SESSION_KEY)
  } catch { /* The demo still works when browser storage is unavailable. */ }
}

export default function App() {
  const [customer, setCustomer] = useState(null)
  const [restoring, setRestoring] = useState(true)
  useEffect(() => {
    let active = true
    let email
    try { email = sessionStorage.getItem(SESSION_KEY) } catch { /* No saved session. */ }
    if (!email) { setRestoring(false); return }
    apiPost('/demo/session', { email }).then(profile => {
      if (active) setCustomer(profile)
    }).catch(() => { if (active) rememberEmail('') })
      .finally(() => { if (active) setRestoring(false) })
    return () => { active = false }
  }, [])
  if (restoring) return <main className="session-loading"><Brand /><p role="status">Opening your savings…</p></main>
  if (!customer) return <Login onSignedIn={profile => { rememberEmail(profile.email); setCustomer(profile) }} />
  return <Dashboard key={customer.id} customer={customer} onSignOut={() => { rememberEmail(''); setCustomer(null) }} />
}

function Dashboard({ customer, onSignOut }) {
  const [tab, setTab] = useState('overview')
  const customerId = customer.id
  const [balance, setBalance] = useState(null)
  const [accounts, setAccounts] = useState(null)
  const [movements, setMovements] = useState([])
  const [depositLots, setDepositLots] = useState({ outstanding_eur: null, lots: [] })
  const [catalogue, setCatalogue] = useState(null)
  const [claims, setClaims] = useState([])
  // The item the customer has asked to claim but not yet confirmed, and the
  // voucher the seam issued for the last confirmed one.
  const [pending, setPending] = useState(null)
  const [lastVoucher, setLastVoucher] = useState(null)
  const [error, setError] = useState(null)
  const [readError, setReadError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [loadedFor, setLoadedFor] = useState(null)
  const [lastEvent, setLastEvent] = useState(null)
  const [busy, setBusy] = useState(false)
  // Every read is numbered, and only the newest number is allowed to paint.
  // Customer changes remount this
  // dashboard; aborted or superseded reads must never paint another snapshot.
  const newestRead = useRef(0)
  const customerRef = useRef(customerId)
  const inFlight = useRef(null)
  // One idempotency key per claim attempt (spec D15). The key is minted when
  // the customer asks to claim an item and kept until that attempt succeeds,
  // so a double-click — or a retry after a refusal or a dropped response —
  // sends the same key and the seam issues exactly one voucher.
  const claimKeys = useRef(new Map())
  const claiming = useRef(false)

  const [deposit, setDeposit] = useState({
    accountId: customer.account_id,
    depositId: newIdempotencyKey(),
    amountEur: '',
  })
  const [withdrawal, setWithdrawal] = useState({
    accountId: customer.account_id,
    withdrawalId: newIdempotencyKey(),
    amountEur: '',
  })

  const refresh = useCallback(async (id) => {
    inFlight.current?.abort()
    const controller = new AbortController()
    inFlight.current = controller
    const read = ++newestRead.current
    const isCurrent = () => read === newestRead.current && id === customerRef.current
    setLoadedFor(null)
    setReadError(null)
    setBalance(null)
    setAccounts(null)
    setMovements([])
    setClaims([])
    setDepositLots({ outstanding_eur: null, lots: [] })
    setLoading(Boolean(id))
    if (!id) return
    try {
      const [balanceBody, historyBody, claimsBody, depositLotsBody, accountsBody] = await Promise.all([
        apiGet(`/customers/${encodeURIComponent(id)}/points/balance`, controller.signal),
        apiGet(`/customers/${encodeURIComponent(id)}/points/history`, controller.signal),
        apiGet(`/customers/${encodeURIComponent(id)}/claims`, controller.signal),
        apiGet(`/customers/${encodeURIComponent(id)}/deposit-lots`, controller.signal),
        apiGet(`/demo/customers/${encodeURIComponent(id)}/accounts`, controller.signal),
      ])
      if (!isCurrent()) return
      setBalance(balanceBody.balance)
      setAccounts(accountsBody)
      setMovements(historyBody.movements)
      setClaims(claimsBody.claims)
      // Dates, amounts and ordering come directly from the existing service.
      setDepositLots(depositLotsBody)
      setLoadedFor({ customerId: id })
    } catch (e) {
      if (e.name !== 'AbortError' && isCurrent()) setReadError(e.message)
    } finally {
      if (isCurrent()) setLoading(false)
    }
  }, [])

  useEffect(() => {
    customerRef.current = customerId
    setError(null)
    setLastEvent(null)
    setLastVoucher(null)
    setPending(null)
    // Keys belong to the customer who was about to spend them.
    claimKeys.current = new Map()
    refresh(customerId)
    return () => inFlight.current?.abort()
  }, [customerId, refresh])

  // The catalogue is the same for everyone, so it is read once, from the seam.
  useEffect(() => {
    const controller = new AbortController()
    apiGet('/catalogue', controller.signal)
      .then(setCatalogue)
      .catch(showUnlessSuperseded)
    return () => controller.abort()
  }, [])

  function showUnlessSuperseded(e) {
    // A superseded read is aborted on purpose; that is not an error to show.
    if (e.name !== 'AbortError') setError(e.message)
  }

  async function send(direction, body) {
    if (busy || !ready) return
    const id = customerId
    setBusy(true)
    setError(null)
    try {
      const result = await apiPost('/demo/transfers', { ...body, direction })
      // The customer box may have moved on while the event was in flight; the
      // result belongs to the customer it was sent for, not to whoever is on
      // screen now.
      if (id !== customerRef.current) return
      setLastEvent({ direction, ...result })
      await refresh(id)
      if (direction === 'deposit') setDeposit(value => ({ ...value, depositId: newIdempotencyKey(), amountEur: '' }))
      if (direction === 'withdrawal') setWithdrawal(value => ({ ...value, withdrawalId: newIdempotencyKey(), amountEur: '' }))
    } catch (e) {
      if (e.name === 'AbortError') return
      setError(e.message)
      setLastEvent(null)
      await refresh(id)
    } finally {
      setBusy(false)
    }
  }

  function keyFor(itemId) {
    if (!claimKeys.current.has(itemId)) claimKeys.current.set(itemId, newIdempotencyKey())
    return claimKeys.current.get(itemId)
  }

  async function confirmClaim() {
    // Guarded twice over: this ref stops a second click before React has
    // re-rendered the disabled button, and the key makes a request that does
    // get through a replay of the first one rather than a second claim.
    if (!pending || claiming.current) return
    claiming.current = true
    const id = customerId
    const item = pending
    setBusy(true)
    setError(null)
    try {
      const result = await apiPost('/claims', {
        customer_id: id,
        item_id: item.item_id,
        idempotency_key: keyFor(item.item_id),
      })
      if (id !== customerRef.current) return
      claimKeys.current.delete(item.item_id)
      setPending(null)
      setLastEvent(null)
      setLastVoucher(result)
      await refresh(id)
    } catch (e) {
      if (e.name === 'AbortError') return
      setError(e.message)
      setLastVoucher(null)
      setPending(null)
    } finally {
      claiming.current = false
      setBusy(false)
    }
  }

  // Identity also protects the render before the customer effect starts.
  const ready = loadedFor?.customerId === customerId
    && !loading && !readError
  const emptyMessage = !customerId ? 'Enter a customer to see their data.'
    : readError ? 'Data unavailable. Retry the request.'
      : 'Loading customer data…'

  return (
    <div className="app">
      <header className="top">
        <Brand />
        <div className="profile-menu"><span className="avatar" aria-hidden="true">{customer.initials}</span>
          <div><strong>{customer.name}</strong><span className="muted">Demo account</span></div>
          <button type="button" className="link" disabled={busy} onClick={onSignOut}>Sign out</button>
        </div>
      </header>
      <nav className="tabs" aria-label="Main navigation">
        {TABS.map(([id, label]) => (
          <button type="button" key={id} className={tab === id ? 'active' : ''}
            aria-current={tab === id ? 'page' : undefined}
            onClick={() => setTab(id)}>{label}</button>
        ))}
      </nav>
      <main id="main-content">
        <h1 className="sr-only">Saving Streak — {customer.name}</h1>
        {error && <p className="refusal" role="alert">{error}</p>}
        {readError && <div className="refusal" role="alert">{readError}{' '}
          <button type="button" className="secondary" onClick={() => refresh(customerRef.current)}>Retry</button>
        </div>}
        {!ready && customerId && !readError && <p className="muted" role="status">Loading customer data…</p>}
        {lastEvent && <p className="said" role="status">
          {lastEvent.replayed ? 'Transfer already recorded' : lastEvent.direction === 'deposit' ? 'Deposit recorded' : 'Withdrawal recorded'}.
          {lastEvent.points_delta !== 0 && <> {lastEvent.points_delta > 0 ? '+' : ''}{lastEvent.points_delta} points.</>}
        </p>}
        {lastVoucher && <p className="said" role="status">Voucher issued for {lastVoucher.item_name}:{' '}
          <code>{lastVoucher.voucher_code}</code>.
          {lastVoucher.replayed ? ' Already processed; no extra charge.' :
            ` ${lastVoucher.price_points} points spent.`}
        </p>}
        {tab === 'overview' && <>
          <div className="page-heading"><h2>Hello, {customer.name.split(' ')[0]}.</h2>
</div>
          <section className="total-balance" aria-label="Total money">
            <span>Total money</span><strong>{ready ? money(accounts.total_eur) : '—'}</strong>
            <span className="muted">Savings + spending</span>
          </section>
          <div className="stats">
            <section className="stat points-stat"><h3 className="stat-label">Points balance</h3>
              <strong className={ready && balance < 0 ? 'negative' : ''}>{ready ? balance : '—'}</strong>
              <span className="muted">Available rewards points</span>
            </section>
            <section className="stat savings-stat"><span className="active-badge">Active account</span>
              <h3 className="stat-label">{customer.account_name}</h3>
              <strong>{ready ? money(accounts.savings_eur) : '—'}</strong>
              <span className="muted account-id">{customer.account_id}</span>
            </section>
            <section className="stat spending-stat"><h3 className="stat-label">Spending account</h3>
              <strong>{ready ? money(accounts.available_eur) : '—'}</strong>
              <span className="muted">Available to deposit</span>
            </section>
          </div>
          <div className="quick-actions">
            <button type="button" onClick={() => setTab('money')}>Move money</button>
            <button type="button" className="secondary" onClick={() => setTab('rewards')}>Explore rewards</button>
          </div>
      <section className="card deposit-lots">
        <h2>Your savings activity</h2>
        {!ready ? <p className="muted">{emptyMessage}</p> : depositLots.lots.length === 0 ? (
          <p className="muted">No deposits yet.</p>
        ) : (
          <>
            <div className="table-scroll" role="region" aria-label="Deposits" tabIndex={0}><table>
              <thead>
                <tr>
                  <th>Deposited</th>
                  <th className="numeric">Deposited amount</th>
                  <th className="numeric">Remaining</th>
                  <th>Next anniversary</th>
                </tr>
              </thead>
              <tbody>
                {depositLots.lots.map((lot) => (
                  <tr key={lot.deposit_id}>
                    <td>{formatDay(lot.deposited_at)}</td>
                    <td className="numeric">€{lot.amount_eur}</td>
                    <td className="numeric">€{lot.outstanding_eur}</td>
                    <td>{formatDay(lot.next_anniversary)}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          </>
        )}
      </section>
        </>}
        {tab === 'money' && <>
          <div className="page-heading"><h2>Move money</h2></div>
          <section className="active-account" aria-label="Active savings account">
            <div><span className="active-badge">Active account</span><h3>{customer.account_name}</h3>
              <span className="muted account-id">{customer.account_id}</span></div>
            <div className="account-balance"><span className="muted">Savings balance</span>
              <strong>{ready ? money(accounts.savings_eur) : '—'}</strong></div>
          </section>
          <section className="money-forms" aria-label="Money forms">
            <form className="card" onSubmit={event => {
              event.preventDefault()
              send('deposit', { customer_id: customerId, account_id: deposit.accountId,
                transfer_id: deposit.depositId, amount_eur: deposit.amountEur })
            }}>
              <fieldset disabled={busy || !ready}>
                <h3>Add to savings</h3>
                <p className="transfer-route">Spending account <span aria-hidden="true">→</span><span className="sr-only">to</span> {customer.account_name}</p>
                <Field label="Amount (€)" type="number" inputMode="decimal" min="0.01" step="0.01"
                  max={ready ? accounts.available_eur : undefined} placeholder="0.00" required
                  aria-describedby="deposit-limit" value={deposit.amountEur}
                  onChange={event => setDeposit({ ...deposit, amountEur: event.target.value, depositId: newIdempotencyKey() })} />
                <p id="deposit-limit" className="transfer-limit">Available to deposit: <strong>{ready ? money(accounts.available_eur) : '—'}</strong></p>
                <button type="submit" disabled={busy || !ready || Number(accounts?.available_eur) <= 0}>{busy ? 'Please wait…' : 'Deposit'}</button>
              </fieldset>
            </form>
            <form className="card" onSubmit={event => {
              event.preventDefault()
              send('withdrawal', { customer_id: customerId, account_id: withdrawal.accountId,
                transfer_id: withdrawal.withdrawalId, amount_eur: withdrawal.amountEur })
            }}>
              <fieldset disabled={busy || !ready}>
                <h3>Withdraw from savings</h3>
                <p className="transfer-route">{customer.account_name} <span aria-hidden="true">→</span><span className="sr-only">to</span> Spending account</p>
                <Field label="Amount (€)" type="number" inputMode="decimal" min="0.01" step="0.01"
                  max={ready ? accounts.savings_eur : undefined} placeholder="0.00" required
                  aria-describedby="withdrawal-limit" value={withdrawal.amountEur}
                  onChange={event => setWithdrawal({ ...withdrawal, amountEur: event.target.value, withdrawalId: newIdempotencyKey() })} />
                <p id="withdrawal-limit" className="transfer-limit">Available to withdraw: <strong>{ready ? money(accounts.savings_eur) : '—'}</strong></p>
                <button type="submit" disabled={busy || !ready || Number(accounts?.savings_eur) <= 0}>{busy ? 'Please wait…' : 'Withdraw'}</button>
              </fieldset>
            </form>
          </section>
        </>}
        {tab === 'rewards' && <>
      <section className="card rewards">
        <h2>Rewards</h2>
        {catalogue === null ? (
          <p className="muted">Loading the catalogue…</p>
        ) : (
          <>
            <div className="table-scroll" role="region" aria-label="Rewards" tabIndex={0}><table>
              <thead>
                <tr>
                  <th>Reward</th>
                  <th className="numeric">Price</th>
                  <th><span className="sr-only">Action</span></th>
                </tr>
              </thead>
              <tbody>
                {catalogue.items.map((item) => (
                  <tr key={item.item_id}>
                    <td>{item.name}</td>
                    <td className="numeric">{item.price_points} points</td>
                    <td className="numeric">
                      <button
                        type="button"
                        disabled={busy || !customerId}
                        onClick={() => {
                          setError(null)
                          setLastVoucher(null)
                          setPending(item)
                        }}
                      >
                        Claim
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          </>
        )}

      </section>
      <section className="card vouchers">
        <h2>Vouchers claimed</h2>
        {!ready ? <p className="muted">{emptyMessage}</p> : claims.length === 0 ? (
          <p className="muted">Nothing claimed yet.</p>
        ) : (
          <div className="table-scroll" role="region" aria-label="Vouchers" tabIndex={0}><table>
            <thead>
              <tr>
                <th>When</th>
                <th>Reward</th>
                <th>Voucher</th>
                <th className="numeric">Paid</th>
              </tr>
            </thead>
            <tbody>
              {claims.map((claim) => (
                <tr key={claim.voucher_code}>
                  <td>{formatMoment(claim.claimed_at)}</td>
                  <td>{claim.item_name}</td>
                  <td className="voucher-code">{claim.voucher_code}</td>
                  <td className="numeric">{claim.price_points}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        )}
      </section>
        </>}
        {tab === 'history' && <>
      <section className="card history">
        <h2>Points history</h2>
        {!ready ? <p className="muted">{emptyMessage}</p> : movements.length === 0 ? (
          <p className="muted">No points movements yet.</p>
        ) : (
          <div className="table-scroll" role="region" aria-label="Points history" tabIndex={0}><table>
            <thead>
              <tr>
                <th>When</th>
                <th>Reason</th>
                <th>What happened</th>
                <th className="numeric">Points</th>
              </tr>
            </thead>
            <tbody>
              {movements.map((movement, index) => (
                <tr key={`${movement.occurred_at}-${index}`}>
                  <td>{formatMoment(movement.occurred_at)}</td>
                  <td>{REASON_LABELS[movement.reason] ?? movement.reason}</td>
                  <td>{movement.description}</td>
                  <td className={movement.points < 0 ? 'numeric negative' : 'numeric'}>
                    {movement.points > 0 ? `+${movement.points}` : movement.points}
                  </td>
                </tr>
              ))}
            </tbody>
          </table></div>
        )}
      </section>
        </>}
        {pending && <ClaimConfirmation item={pending} busy={busy}
          onConfirm={confirmClaim} onCancel={() => setPending(null)} />}
      </main>
    </div>
  )
}
