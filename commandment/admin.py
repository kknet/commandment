"""
Copyright (c) 2015 Jesse Peterson
Licensed under the MIT license. See the included LICENSE.txt file for details.
"""

from flask import Blueprint, render_template, Response, request, redirect, current_app, abort, make_response
#from .pki.certificateauthority import get_ca
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

from .models import CertificateType, Device
from .models import Certificate as DBCertificate, RSAPrivateKey as DBPrivateKey
from .profiles.models import Profile as DBProfile
from .models import App
from .profiles.restrictions import RestrictionsPayload
from .profiles import Profile
from .mdmcmds import InstallProfile, RemoveProfile, AppInstall
from .push import push_to_device
import uuid
import os
from .utils.app_manifest import pkg_signed, get_pkg_bundle_ids, get_chunked_md5, MD5_CHUNK_SIZE
import tempfile
from shutil import copyfile
from email.parser import Parser
import json
from .utils.dep import DEP
from .utils.dep_utils import initial_fetch, mdm_profile, assign_devices
import datetime
from urllib.parse import urlparse
from base64 import b64encode


class FixedLocationResponse(Response):
    # override Werkzeug default behaviour of "fixing up" once-non-compliant
    # relative location headers. now permitted in rfc7231 sect. 7.1.2
    autocorrect_location_header = False


admin_app = Blueprint('admin_app', __name__)


@admin_app.route('/')
def index():
    return render_template('index.html')


def install_group_profiles_to_device(group, device):
    q = db_session.query(DBProfile.id).join(profile_group_assoc).filter(profile_group_assoc.c.mdm_group_id == group.id)

    # note singular tuple for subject here
    for profile_id, in q:
        new_qc = InstallProfile.new_queued_command(device, {'id': profile_id})
        db_session.add(new_qc)


def remove_group_profiles_from_device(group, device):
    q = db_session.query(DBProfile.identifier).join(profile_group_assoc).filter(
        profile_group_assoc.c.mdm_group_id == group.id)

    # note singular tuple for subject here
    for profile_identifier, in q:
        print('Queueing removal of profile identifier:', profile_identifier)
        new_qc = RemoveProfile.new_queued_command(device, {'Identifier': profile_identifier})
        db_session.add(new_qc)


@admin_app.route('/device/<int:device_id>/groupmod', methods=['POST'])
def admin_device_groupmod(device_id):
    # get device info
    device = db_session.query(Device).filter(Device.id == device_id).one()

    # get list of unique group IDs to be assigned
    new_group_memberships = set([int(g_id) for g_id in request.form.getlist('group_membership')])

    # get all MDMGroups left joining against our assoc. table to see if this device is in any of those groups
    group_q = db_session.query(MDMGroup, device_group_assoc.c.device_id).outerjoin(device_group_assoc, and_(
        device_group_assoc.c.mdm_group_id == MDMGroup.id, device_group_assoc.c.device_id == device.id))

    group_additions = []
    group_removals = []
    for group, dev_id in group_q:
        if dev_id:
            # this device is in this group currently
            if group.id not in new_group_memberships:
                # this device is being removed from this group!
                print('Device %d is being REMOVED from Group %d (%s)!' % (device.id, group.id, group.group_name))
                group_removals.append(group)
                # else:
                #   print 'Device %d is REMAINING in Group %d (%s)!' % (device.id, group.id, group.group_name)
        else:
            # this device is NOT in this group currently
            if group.id in new_group_memberships:
                print('Device %d is being ADDED to Group %d (%s)!' % (device.id, group.id, group.group_name))
                group_additions.append(group)
                # else:
                #   print 'Device %d is REMAINING out of Group %d (%s)!' % (device.id, group.id, group.group_name)

    # get all groups
    groups = db_session.query(MDMGroup)

    # select the groups that match the new membership ids and assign to device
    device.mdm_groups = [g for g in groups if g.id in new_group_memberships]

    # commit our changes
    db_session.commit()

    for i in group_additions:
        install_group_profiles_to_device(i, device)

    for i in group_removals:
        remove_group_profiles_from_device(i, device)

    if group_removals or group_additions:
        db_session.commit()
        push_to_device(device)

    return redirect('/admin/device/%d' % int(device.id), Response=FixedLocationResponse)


@admin_app.route('/device/<int:device_id>/appinst', methods=['POST'])
def admin_device_appinst(device_id):
    # get device info
    device = db_session.query(Device).filter(Device.id == device_id).one()

    # get app id
    app_id = int(request.form['application'])

    # note singular tuple for subject here
    new_appinst = AppInstall.new_queued_command(device, {'id': app_id})
    db_session.add(new_appinst)
    db_session.commit()
    push_to_device(device)

    return redirect('/admin/device/%d' % device_id, Response=FixedLocationResponse)


@admin_app.route('/apps', methods=['GET'])
def admin_app_list():
    apps = db_session.query(App)
    return render_template('admin/apps.html', apps=apps)


@admin_app.route('/app/add', methods=['POST'])
def admin_app_add():
    new_file = request.files['app']

    if not new_file.filename.endswith('.pkg'):
        abort(400,
              'Failed: filename does not end with ".pkg". Upload must be an Apple Developer-signed Apple "flat" package installer.')

    # first, save the file to a temporary location. ideally we'd like to read
    # the temporary file it's already contained in but Werkzeug only seems to
    # give us access to the file handle of that (.stream attribute) and not a
    # filename that we need. this implies we'll need to copy the file twice
    # (once out of the temporary stream to this temp file, then once into the
    # uploaded location).
    temp_file_handle, temp_file_path = tempfile.mkstemp()

    new_file.save(temp_file_path)

    if not pkg_signed(temp_file_path):
        os.close(temp_file_handle)
        os.unlink(temp_file_path)
        abort(400,
              'Failed: uploaded package not signed. Upload must be an Apple Developer-signed Apple "flat" package installer.')

    # get MD5 and MD5 chunks
    md5, md5s = get_chunked_md5(temp_file_path, chunksize=MD5_CHUNK_SIZE)

    # get bundle and package IDs
    pkg_ids, bundle_ids = get_pkg_bundle_ids(temp_file_path)

    filesize = os.path.getsize(temp_file_path)

    new_app = App()
    new_app.filename = new_file.filename
    new_app.filesize = filesize

    new_app.md5_hash = md5
    new_app.md5_chunk_size = MD5_CHUNK_SIZE
    new_app.md5_chunk_hashes = ':'.join(md5s)

    new_app.pkg_ids_json = pkg_ids
    new_app.bundle_ids_json = bundle_ids

    db_session.add(new_app)
    db_session.commit()

    apps_dir = os.path.join(current_app.root_path, current_app.config['APP_UPLOAD_ROOT'])

    if not os.path.isdir(apps_dir):
        os.mkdir(apps_dir)

    new_file_path = os.path.join(apps_dir, new_app.path_format())

    copyfile(temp_file_path, new_file_path)
    # new_file.save(new_file_path)

    # remove the temp file
    os.close(temp_file_handle)
    os.unlink(temp_file_path)

    return redirect('/admin/apps', Response=FixedLocationResponse)


@admin_app.route('/app/delete/<int:app_id>', methods=['GET'])
def admin_app_delete(app_id):
    app_q = db_session.query(App).filter(App.id == app_id)
    app = app_q.one()

    apps_dir = os.path.join(current_app.root_path, current_app.config['APP_UPLOAD_ROOT'])

    try:
        os.unlink(os.path.join(apps_dir, app.path_format()))
    except OSError:
        # just continue on -- best effort for deletion
        pass

    db_session.delete(app)
    db_session.commit()

    return redirect('/admin/apps', Response=FixedLocationResponse)


@admin_app.route('/app/manage/<int:app_id>', methods=['GET'])
def admin_app_manage(app_id):
    app = db_session.query(App).filter(App.id == app_id).one()

    # get all MDMGroups left joining against our assoc. table to see if this device is in any of those groups
    group_q = db_session.query(
        MDMGroup,
        app_group_assoc.c.app_id,
        app_group_assoc.c.install_early). \
        outerjoin(
        app_group_assoc,
        and_(
            app_group_assoc.c.mdm_group_id == MDMGroup.id,
            app_group_assoc.c.app_id == app_id))

    groups = [dict(list(zip(('group', 'app_id', 'install_early',), r))) for r in group_q]

    return render_template('admin/app_manage.html', app=app, groups=groups)


@admin_app.route('/app/manage/<int:app_id>/groupmod', methods=['POST'])
def admin_app_manage_groupmod(app_id):
    app = db_session.query(App).filter(App.id == app_id).one()

    q = db_session.query(
        app_group_assoc.c.mdm_group_id,
        app_group_assoc.c.install_early). \
        filter(app_group_assoc.c.app_id == app.id)

    app_groups = dict(q.all())

    new_app_groups = {}

    form_groups = request.form.getlist('group_id', type=int)

    for gid in form_groups:
        if gid not in new_app_groups:
            new_app_groups[gid] = False

    form_ie = request.form.getlist('install_early', type=int)

    for gid in form_ie:
        if gid in new_app_groups:
            new_app_groups[gid] = True

    before_groups = set(app_groups.keys())
    after_groups = set(new_app_groups.keys())

    gm_delete = before_groups.difference(after_groups)
    gm_same = before_groups.intersection(after_groups)
    gm_add = after_groups.difference(before_groups)

    for same_id in gm_same:
        q = update(app_group_assoc). \
            values(install_early=bool(new_app_groups.get(same_id))). \
            where(and_(app_group_assoc.c.app_id == app.id, app_group_assoc.c.mdm_group_id == same_id))
        db_session.execute(q)

    if gm_delete:
        q = delete(app_group_assoc). \
            where(and_(app_group_assoc.c.app_id == app.id, app_group_assoc.c.mdm_group_id.in_(gm_delete)))
        db_session.execute(q)

    for add_id in gm_add:
        q = insert(app_group_assoc).values(
            app_id=app_id,
            mdm_group_id=add_id,
            install_early=bool(new_app_groups.get(add_id)))

        db_session.execute(q)

    db_session.commit()

    return redirect('/admin/apps', Response=FixedLocationResponse)


@admin_app.route('/config/edit', methods=['GET', 'POST'])
def admin_config():
    config = db_session.query(MDMConfig).first()
    scep_config = db_session.query(SCEPConfig).first()

    if not config:
        return redirect('/admin/config/add', Response=FixedLocationResponse)

    existing_hostname = urlparse(config.base_url()).hostname
    existing_scep_hostname = '' if not config.scep_url else urlparse(config.scep_url).hostname

    if existing_scep_hostname == existing_hostname:
        existing_scep_hostname = ''

    if request.method == 'POST':
        config.ca_cert_id = int(request.form['ca_cert'])
        config.mdm_name = request.form['name']
        config.description = request.form['description'] if request.form['description'] else None

        config.device_identity_method = request.form.get('device_identity_method')

        if config.device_identity_method == 'ourscep':
            frm_scep_hostname = request.form.get('ourscep_hostname')
            scep_hostname = frm_scep_hostname if frm_scep_hostname else urlparse(config.base_url()).hostname
            config.scep_url = 'http://%s:%d' % (scep_hostname, current_app.config.get('SCEP_PORT'))
        elif config.device_identity_method == 'provide':
            config.scep_url = None
            config.scep_challenge = None
        else:
            abort(400, 'Invalid device identity method')

        db_session.commit()
        return redirect('/admin/config/edit', Response=FixedLocationResponse)
    else:
        ca_certs = db_session.query(DBCertificate).join(DBPrivateKey.certificates).filter(
            DBCertificate.cert_type == 'mdm.cacert')
        for i in ca_certs:
            i.subject_text = i.to_x509().get_subject_text()
        return render_template(
            'admin/config/edit.html',
            config=config,
            ca_certs=ca_certs,
            scep_port=current_app.config.get('SCEP_PORT'),
            scep_present=bool(scep_config),
            device_identity_method=config.device_identity_method,
            ourscep_hostname=existing_scep_hostname)


@admin_app.route('/dep/')
@admin_app.route('/dep/index')
def dep_index():
    dep_configs = db_session.query(DEPConfig)
    dep_profiles = db_session.query(DEPProfile)
    return render_template('admin/dep/index.html', dep_configs=dep_configs, dep_profiles=dep_profiles)


@admin_app.route('/dep/add')
def dep_add():
    new_dep = DEPConfig()

    ca_cert = DBCertificate.find_one_by_cert_type('mdm.cacert')

    new_dep.certificate = ca_cert

    db_session.add(new_dep)
    db_session.commit()

    return redirect('/admin/dep/index', Response=FixedLocationResponse)


@admin_app.route('/dep/manage/<int:dep_id>')
def dep_manage(dep_id):
    dep = db_session.query(DEPConfig).filter(DEPConfig.id == dep_id).one()
    return render_template('admin/dep/manage.html', dep_config=dep)


@admin_app.route('/dep/cert/<int:dep_id>/DEP_MDM.crt')
def dep_cert(dep_id):
    dep = db_session.query(DEPConfig).filter(DEPConfig.id == dep_id).one()
    # TODO: better to use a join rather than two queries
    response = make_response(dep.certificate.pem_certificate)
    # TODO: technically we really ought to use a proper MIME type but since
    # some browsers do fancy stuff when downloading properly typed certs for
    # now just use an octet-stream
    response.headers['Content-Type'] = 'application/octet-stream'
    response.headers['Content-Disposition'] = 'attachment; filename=DEP_MDM.crt'
    return response


@admin_app.route('/dep/tokenupload/<int:dep_id>', methods=['POST'])
def dep_tokenupload(dep_id):
    # get DEP config
    dep = db_session.query(DEPConfig).filter(DEPConfig.id == dep_id).one()

    filedata = request.files['server_token_file'].stream.read()

    try:
        smime = filedata

        # load the encrypted file
        p7, data = SMIME.smime_load_pkcs7_bio(BIO.MemoryBuffer(str(smime)))

        # query DB to get cert & key from DB
        q = db_session.query(DBCertificate, DBPrivateKey).join(DBCertificate, DBPrivateKey.certificates).filter(
            DBCertificate.id == dep.certificate.id)
        cert, pk = q.one()

        # construct SMIME object using cert & key
        decryptor = SMIME.SMIME()
        decryptor.load_key_bio(BIO.MemoryBuffer(str(pk.pem_key)), BIO.MemoryBuffer(str(cert.pem_certificate)))

        # decrypt!
        out = decryptor.decrypt(p7)

        eml = Parser().parsestr(out).get_payload()

        if eml.startswith('-----BEGIN MESSAGE-----\n') and eml.endswith('\n-----END MESSAGE-----\n'):
            myjson = eml[24:-23]
    except SMIME.SMIME_Error:
        # submitted file was not an SMIME encrypted file
        # try to just load the file in the hopes the json parser can read it
        myjson = filedata

    try:
        json_loaded = json.loads(myjson)

        dep.server_token = json_loaded
        db_session.commit()
    except ValueError:
        abort(400, 'Invalid server token supplied')

    return redirect('/admin/dep/index', Response=FixedLocationResponse)


@admin_app.route('/dep/profile/add', methods=['GET', 'POST'])
def dep_profile_add():
    if request.method == 'POST':
        form_bools = ('allow_pairing', 'is_supervised', 'is_multi_user', 'is_mandatory', 'await_device_configured',
                      'is_mdm_removable')
        form_strs = ('profile_name', 'support_phone_number', 'support_email_address', 'department', 'org_magic')

        profile = {}

        # go through submitted bools and convert to actual bools in the dict
        for form_bool in form_bools:
            if form_bool in request.form:
                profile[form_bool] = request.form.get(form_bool, type=bool)

        # go through submitted strs and convert to actual bools in the dict
        for form_str in form_strs:
            if form_str in request.form and request.form.get(form_str):
                profile[form_str] = request.form.get(form_str)

        if not 'profile_name' in profile:
            raise Exception('DEP profile must have profile_name')

        # gather our skip_setup_items from the form
        if 'skip_setup_items' in request.form:
            profile['skip_setup_items'] = request.form.getlist('skip_setup_items')

        # TODO: await_device_configured

        dep = db_session.query(DEPConfig).filter(DEPConfig.id == request.form.get('dep_config_id', type=int)).one()
        mdm = db_session.query(MDMConfig).filter(MDMConfig.id == request.form.get('mdm_config_id', type=int)).one()

        profile['url'] = mdm.base_url() + '/enroll'

        # find and include all mdm.webcrt's
        # TODO: find actual cert chain rather than specific web cert
        q = db_session.query(DBCertificate).filter(DBCertificate.cert_type == 'mdm.webcrt')
        anchor_certs = [b64encode(cert.to_x509().to_der()) for cert in q]

        if anchor_certs:
            profile['anchor_certs'] = anchor_certs

        new_dep_profile = DEPProfile()

        new_dep_profile.mdm_config = mdm
        new_dep_profile.dep_config = dep

        new_dep_profile.profile_data = profile

        # TODO: supervising_host_certs
        # TODO: initial list of devices?

        db_session.add(new_dep_profile)
        db_session.commit()

        return redirect('/admin/dep/index', Response=FixedLocationResponse)

    else:
        mdms = db_session.query(MDMConfig)
        deps = db_session.query(DEPConfig)
        return render_template('admin/dep/add_profile.html', dep_configs=deps, mdm_configs=mdms,
                               initial_magic=uuid.uuid4())


@admin_app.route('/dep/profile/manage/<int:profile_id>', methods=['GET', 'POST'])
def dep_profile_manage(profile_id):
    dep_profile = db_session.query(DEPProfile).filter(DEPProfile.id == profile_id).one()
    if request.method != 'POST':
        return render_template('admin/dep/manage_profile.html', dep_profile=dep_profile)
    else:
        submitted_dev_ids = [int(i) for i in request.form.getlist('devices')]
        if len(submitted_dev_ids):
            devices = db_session.query(Device).filter(
                and_(Device.dep_config == dep_profile.dep_config, or_(*[Device.id == i for i in submitted_dev_ids])))
            assign_devices(dep_profile, devices)
        return redirect('/admin/dep/index', Response=FixedLocationResponse)


@admin_app.route('/dep/test1/<int:dep_id>')
def dep_test1(dep_id):
    # get DEP config
    mdm = db_session.query(MDMConfig).one()
    dep = db_session.query(DEPConfig).filter(DEPConfig.id == dep_id).one()

    initial_fetch(dep)

    return 'initial_fetch complete'
    # return '<pre>%s</pre>' % str(mdm_profile(mdm))
