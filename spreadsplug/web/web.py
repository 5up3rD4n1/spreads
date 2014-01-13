import json
import logging
import shutil
import tempfile
import time
import zipfile

import requests
from flask import (abort, jsonify, request, send_file, make_response,
                   render_template)
from wand.image import Image
from werkzeug import secure_filename

import spreads.vendor.confit as confit
from spreads.plugin import (get_pluginmanager, get_relevant_extensions,
                            get_driver)
from spreads.vendor.pathlib import Path
from spreads.workflow import Workflow

import persistence
from spreadsplug.web import app
from util import cached, get_image_url, workflow_to_dict, WorkflowConverter


logger = logging.getLogger('spreadsplub.web')


# Register custom workflow converter for URL routes
app.url_map.converters['workflow'] = WorkflowConverter


# ========= #
#  General  #
# ========= #
@app.route('/')
def index():
    """ Deliver static landing page that launches the client-side app. """
    return render_template("index.html", debug=app.config['DEBUG'])


@app.route('/plugins', methods=['GET'])
def get_plugins_with_options():
    """ Return the names of all activated plugins and their configuration
        templates.
    """
    config = app.config['default_config']
    pluginmanager = get_pluginmanager(config)
    scanner_extensions = ['prepare_capture', 'capture', 'finish_capture']
    processor_extensions = ['process', 'output']
    if app.config['mode'] == 'scanner':
        templates = {ext.name: ext.plugin.configuration_template()
                     for ext in get_relevant_extensions(
                         pluginmanager, scanner_extensions)}
        templates["device"] = (get_driver(config["driver"].get())
                               .driver.configuration_template())
    elif app.config['mode'] == 'processor':
        templates = {ext.name: ext.plugin.configuration_template()
                     for ext in get_relevant_extensions(
                         pluginmanager, processor_extensions)}
    elif app.config['mode'] == 'full':
        templates = {ext.name: ext.plugin.configuration_template()
                     for ext in get_relevant_extensions(
                         pluginmanager,
                         scanner_extensions + processor_extensions)}
        templates["device"] = (get_driver(config["driver"].get())
                               .driver.configuration_template())
    rv = dict()
    for plugname, options in templates.iteritems():
        if options is None:
            continue
        rv[plugname] = {key: dict(value=option.value,
                                  docstring=option.docstring,
                                  selectable=option.selectable)
                        for key, option in options.iteritems()}
    return jsonify(rv)


@app.route('/config', methods=['GET'])
def get_global_config():
    """ Return the global configuration. """
    return jsonify(app.config['default_config'].flatten())


# ================== #
#  Workflow-related  #
# ================== #
@app.route('/workflow', methods=['POST'])
def create_workflow():
    """ Create a new workflow.

    Payload should be a JSON object. The only required attribute is 'name' for
    the desired workflow name. Optionally, 'config' can be set to a
    configuration object in the form "plugin_name: { setting: value, ...}".

    Returns the newly created workflow as a JSON object.
    """
    data = json.loads(request.data)
    path = Path(app.config['base_path'])/unicode(data['name'])

    # Setup default configuration
    config = confit.Configuration('spreads')
    # Overlay user-supplied values, if existant
    user_config = data.get('config', None)
    if user_config is not None:
        config.set(user_config)
    workflow = Workflow(config=config, path=path,
                        step=data.get('step', None),
                        step_done=data.get('step_done', None))
    workflow.id = persistence.save_workflow(workflow)
    return jsonify(workflow_to_dict(workflow))


@app.route('/workflow', methods=['GET'])
def list_workflows():
    """ Return a list of all workflows. """
    workflows = persistence.get_all_workflows()
    return make_response(
        json.dumps([workflow_to_dict(workflow)
                   for workflow in workflows.values()]),
        200, {'Content-Type': 'application/json'})


@app.route('/workflow/<workflow:workflow>', methods=['GET'])
def get_workflow(workflow):
    """ Return a single workflow. """
    return jsonify(workflow_to_dict(workflow))


@app.route('/workflow/<workflow_workflow>', methods=['PUT'])
def update_workflow(workflow):
    """ Update a single workflow.

    Payload should be a JSON object, as returned by the '/workflow/<id>'
    endpoint.
    Currently the only attribute that can be updated from the client
    is the configuration.

    Returns the updated workflow as a JSON object.
    """
    # TODO: Support renaming a workflow, i.e. rename directory as well
    config = json.loads(request.data).get('config', None)
    # Update workflow configuration
    workflow.config.set(config)
    # Persist to disk
    persistence.update_workflow_config(workflow.id, workflow.config)
    return jsonify(workflow_to_dict(workflow))


@app.route('/workflow/<workflow:workflow>', methods=['DELETE'])
def delete_workflow(workflow):
    """ Delete a single workflow from database and disk. """
    # Remove directory
    try:
        shutil.rmtree(unicode(workflow.path))
    except OSError:
        logger.warning("Workflow path {0} could not be removed"
                       .format(workflow.path))
    # Remove from database
    persistence.delete_workflow(workflow.id)
    return jsonify({})


@app.route('/workflow/<workflow:workflow>/poll', methods=['GET'])
def poll_for_updates(workflow):
    """ Stall until the requested workflow has changed.

    Returns the changed workflow as a JSON object.
    """
    old_num_caps = len(workflow.images)
    old_step = workflow.step
    old_step_done = workflow.step_done

    if app.config['DEBUG']:
        # NOTE: The wsgiref server does not time out long-running requests,
        #       so we enforce a timeout of 110 seconds.
        start_time = time.time()
        can_continue = lambda: (time.time() - start_time) < 110
    else:
        can_continue = lambda: True

    while can_continue():
        updated = (len(workflow.images) != old_num_caps
                   or workflow.step != old_step
                   or workflow.step_done != old_step_done)
        if updated:
            return jsonify(workflow_to_dict(workflow))
        else:
            old_num_caps = len(workflow.images)
            old_step = workflow.step
            old_step_done = workflow.step_done
            time.sleep(0.1)
    abort(408)  # Request Timeout


@app.route('/workflow/<workflow:workflow>/download', methods=['GET'])
def download_workflow(workflow):
    """ Return a ZIP archive of the current workflow.

    Included all files from the workflow folder as well as the workflow
    configuration as a YAML dump.
    """
    tmp_path = Path(tempfile.gettempdir())/'spreads_download.zip'
    # Clean up previous file
    if tmp_path.exists:
        tmp_path.unlink()
    with zipfile.ZipFile(unicode(tmp_path), mode='w') as archive:
        # Find all files within up to two levels deep, relative to the
        # workflow base path
        for fpath in workflow.path.glob('**/*'):
            extract_path = '/'.join((workflow.path.stem,
                                     unicode(fpath.relative_to(workflow.path)))
                                    )
            logger.debug("Adding {0} to archive as {1}"
                         .format(fpath, extract_path))
            archive.write(unicode(fpath), extract_path)
        archive.writestr('/'.join((workflow.path.stem, 'config.yaml')),
                         workflow.config.dump())
    return send_file(unicode(tmp_path),
                     attachment_filename=(workflow.path.stem + ".zip"),
                     as_attachment=True)


@app.route('/workflow/<workflow:workflow>/submit', methods=['POST'])
def submit_workflow(workflow):
    """ Submit the requested workflow to the postprocessing server.

    Only available in 'scanner' mode. Requires that the 'postproc_server'
    option is set to the address of a server with the server in 'processor'
    or 'full' mode running.
    """
    if app.config['mode'] not in ('scanner'):
        abort(404)
    server = app.config['postproc_server']
    if not server:
        logger.error("Remote server was not configured, please set the"
                     "'postprocessing_server' value in your configuration!")
        abort(500)
    logger.debug("Creating new workflow on postprocesing server")
    resp = requests.post(server+'/workflow', data=json.dumps(
        {'name': workflow.path.stem,
         'step': 'capture',
         'step_done': True}))
    if not resp:
        logger.error("Error creating remote workflow:\n{0}"
                     .format(resp.content))
        abort(resp.code)
    remote_id = resp.json['id']
    for imgpath in workflow.images:
        logger.debug("Uploading image {0} to postprocessing server"
                     .format(imgpath))
        resp = requests.post("/".join([server, 'workflow', remote_id,
                                       'image']),
                             files={'file': {imgpath.name: imgpath.read('rb')}}
                             )
        if not resp:
            logger.error("Error uploading image {0} to postprocessing server:"
                         " \n{1}".format(imgpath, resp.content))
            abort(resp.code)
    resp = requests.post(server+'/queue', data=json.dumps({'id': remote_id}))
    if not resp:
        logger.error("Error putting remote workflow {1} into job queue:: \n{1}"
                     .format(imgpath, resp.content))
        abort(resp.code)
    return


# =============== #
#  Queue-related  #
# =============== #
@app.route('/queue', methods=['POST'])
def add_to_queue():
    """ Add a workflow to the processing queue.

    Requires the payload to be a workflow object in JSON notation.
    Returns the queue id.
    """
    data = json.loads(request.data)
    pos = persistence.append_to_queue(data['id'])
    return jsonify({'queue_position': pos})


@app.route('/queue', methods=['GET'])
def list_jobs():
    """ List all items in the processing queue. """
    return json.dumps(persistence.get_queue())


@app.route('/queue/<int:queue_pos>', methods=['DELETE'])
def remove_from_queue(pos_idx):
    """ Remove the requested workflow from the processing queue. """
    persistence.delete_from_queue(pos_idx)
    return


# =============== #
#  Image-related  #
# =============== #
@app.route('/workflow/<workflow:workflow>/image', methods=['POST'])
def upload_workflow_image(workflow):
    """ Obtain an image for the requested workflow.

    Image must be sent as an attachment with a valid filename and be in either
    JPG or DNG format. Image will be stored in the 'raw' subdirectory of the
    workflow directory.
    """
    allowed = lambda x: x.rsplit('.', 1)[1].lower() in ('jpg', 'jpeg', 'dng')
    file = request.files['file']
    save_path = workflow.path/'raw'
    if not save_path.exists():
        save_path.mkdir()
    if file and allowed(file.filename):
        filename = secure_filename(file.filename)
        file.save(save_path/filename)
        return "OK"


@app.route('/workflow/<workflow:workflow>/image/<int:img_num>',
           methods=['GET'])
def get_workflow_image(workflow, img_num):
    """ Return image from requested workflow. """
    try:
        img_path = workflow.images[img_num]
    except IndexError:
        abort(404)
    return send_file(unicode(img_path))


@cached
@app.route('/workflow/<workflow:workflow>/image/<int:img_num>/thumb',
           methods=['GET'])
def get_workflow_image_thumb(workflow, img_num):
    """ Return thumbnail for image from requested workflow. """
    thumbfile = (Path(app.config['temp_dir']) /
                 "{0}_{1}.jpg".format(workflow.id, img_num))
    try:
        img_path = workflow.images[img_num]
    except IndexError:
        abort(404)
    if not thumbfile.exists():
        logger.debug('Generating thumbnail for {0}'.format(thumbfile.stem))
        with Image(filename=unicode(img_path)) as img:
            thumb_width = int(300/(img.width/float(img.height)))
            img.sample(300, thumb_width)
            img.save(filename=unicode(thumbfile))
    return send_file(unicode(thumbfile))


# ================= #
#  Capture-related  #
# ================= #
@app.route('/workflow/<workflow:workflow>/capture', methods=['POST'])
def trigger_capture(workflow):
    """ Trigger a capture on the requested workflow.

    Optional parameter 'retake' specifies if the last shot is to be retaken.

    Returns the number of pages shot and a list of the images captured by
    this call in JSON notation.
    """
    if app.config['mode'] not in ('scanner', 'full'):
        abort(404)
    if workflow.step != 'capture':
        workflow.prepare_capture()
    workflow.capture(retake=('retake' in request.args))
    return jsonify({
        'pages_shot': len(workflow.images),
        'images': [get_image_url(workflow, x)
                   for x in workflow.images[-2:]]
    })


@app.route('/workflow/<workflow:workflow>/capture/finish', methods=['POST'])
def finish_capture(workflow):
    """ Wrap up capture process on the requested workflow. """
    if app.config['mode'] not in ('scanner', 'full'):
        abort(404)
    workflow.finish_capture()
    return 'OK'
