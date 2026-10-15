// The UniFi Network application's MongoDB user, created when the dev stack's
// database is first initialised (see docker-compose.dev.yml).
db.getSiblingDB('admin').createUser({
  user: 'unifi',
  pwd: 'unifi',
  roles: ['ace', 'ace_stat', 'ace_audit', 'ace_restore'].map(db => ({db: db, role: 'dbOwner'})),
});
