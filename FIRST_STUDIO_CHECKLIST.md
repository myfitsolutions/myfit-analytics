# First Studio Launch Checklist

## Before Invite

- [ ] Production deployment is healthy
- [ ] `/health` returns `{"status":"ok"}`
- [ ] `/ready` returns `{"status":"ready"}`
- [ ] `python -m app.migrate` completed successfully
- [ ] `APP_ENV=production`
- [ ] Strong production `SESSION_SECRET` configured
- [ ] `SESSION_COOKIE_SECURE=true`
- [ ] HTTPS works end to end
- [ ] Studio and Owner created from a trusted shell
- [ ] Studio settings reviewed
- [ ] Email sending tested if SMTP is enabled
- [ ] Deterministic AI fallback tested
- [ ] OpenAI generation tested only if enabled
- [ ] Supabase/PostgreSQL backup and recovery strategy confirmed with the provider
- [ ] A restore procedure has been reviewed or tested

## Studio Setup

- [ ] Owner login succeeds
- [ ] Owner completes onboarding
- [ ] Timezone is correct
- [ ] Currency is correct
- [ ] Retention thresholds are agreed
- [ ] Default follow-up interval is agreed
- [ ] CSV Data Source profile created if useful
- [ ] Import mapping presets reviewed
- [ ] First Member import completed
- [ ] First Booking import completed
- [ ] First Payment import completed

## Verification

- [ ] Members CRM and member histories are correct
- [ ] Retention Health reflects attended bookings
- [ ] Revenue and Revenue Trend are correct
- [ ] Payment Recovery reflects failed payments
- [ ] Action Center priorities are sensible
- [ ] Follow-Ups work
- [ ] Manual email works if enabled
- [ ] Owner/Manager/Staff permissions verified
- [ ] Import History records the correct counts and source
- [ ] Rollback tested with a disposable test batch
- [ ] Test data removed using exact batch rollback

## After Launch

- [ ] Review application logs and request IDs
- [ ] Review failed or partially completed imports
- [ ] Review email failures if SMTP is enabled
- [ ] Verify analytics with the studio Owner
- [ ] Confirm provider backups remain configured and monitored
