# Sweeps any attachment still on local/db storage onto the configured
# default fs.storage (ir.attachment.force_storage() ->
# _force_storage_to_object_storage(), OCA/storage
# fs_attachment/models/ir_attachment.py:738-746/836 - the supported
# migration primitive). Run AFTER fs_storage_upsert.py has made an fs
# storage the default, and BEFORE the pod/Job that wrote any earlier
# attachments to its own local filesystem exits - once that pod is gone,
# whatever it wrote to its emptyDir is gone with it.
#
# Static (no Jinja substitution needed) - see tasks/_ensure-platform.yml's
# fs-storage-scripts ConfigMap.
env["ir.attachment"].force_storage()
env.cr.commit()
