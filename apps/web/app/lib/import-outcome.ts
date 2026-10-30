/**
 * An import in which the server did not acknowledge every record. `saved` counts
 * acknowledged records; `unsaved` holds the input positions, in order, of the rest.
 * An unacknowledged record may still exist remotely when a response was lost, so
 * `uncertain` is false only when nothing was sent at all.
 */
export class ImportIncompleteError extends Error {
  constructor(readonly saved: number, readonly unsaved: number[], readonly uncertain: boolean) {
    super(`Imported ${saved} of ${saved + unsaved.length} records`)
    this.name = 'ImportIncompleteError'
  }
}
