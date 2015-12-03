import os
import sys
import json
import time
import click
import pkg_resources

from .i18n import get_default_lang, is_valid_language
from .utils import secure_url
from .project import Project


version = pkg_resources.get_distribution('Lektor').version


def echo_json(data):
    click.echo(json.dumps(data, indent=2).rstrip())


class Context(object):

    def __init__(self):
        self._project_path = None
        self._project = None
        self._env = None
        self._ui_lang = None

    def _get_ui_lang(self):
        rv = self._ui_lang
        if rv is None:
            rv = self._ui_lang = get_default_lang()
        return rv

    def _set_ui_lang(self, value):
        self._ui_lang = value

    ui_lang = property(_get_ui_lang, _set_ui_lang)
    del _get_ui_lang, _set_ui_lang

    def set_project_path(self, value):
        self._project_path = value
        self._project = None

    def get_project(self, silent=False):
        if self._project is not None:
            return self._project
        if self._project_path is not None:
            rv = Project.from_path(self._project_path)
        else:
            rv = Project.discover()
        if rv is None:
            if silent:
                return None
            raise click.UsageError('Could not find project')
        self._project = rv
        return rv

    def get_default_output_path(self):
        return self.get_project().get_output_path()

    def get_env(self):
        if self._env is not None:
            return self._env
        from lektor.environment import Environment
        env = Environment(self.get_project(), load_plugins=False)
        self._env = env
        return env

    def load_plugins(self, reinstall=False):
        from .packages import load_packages
        from .pluginsystem import initialize_plugins
        load_packages(self.get_env(), reinstall=reinstall)
        initialize_plugins(self.get_env())


pass_context = click.make_pass_decorator(Context, ensure=True)


def validate_language(ctx, param, value):
    if value is not None and not is_valid_language(value):
        raise click.BadParameter('Unsupported language "%s".' % value)
    return value


@click.group()
@click.option('--project', type=click.Path(),
              help='The path to the lektor project to work with.')
@click.option('--language', default=None, callback=validate_language,
              help='The UI language to use (overrides autodetection).')
@click.version_option(prog_name='Lektor', version=version)
@pass_context
def cli(ctx, project=None, language=None):
    """The lektor management application.

    This command can invoke lektor locally and serve up the website.  It's
    intended for local development of websites.
    """
    if language is not None:
        ctx.ui_lang = language
    if project is not None:
        ctx.set_project_path(project)


@cli.command('build')
@click.option('-O', '--output-path', type=click.Path(), default=None,
              help='The output path.')
@click.option('--watch', is_flag=True, help='If this is enabled the build '
              'process goes into an automatic loop where it watches the '
              'file system for changes and rebuilds.')
@click.option('--prune/--no-prune', default=True, help='Controls if old '
              'artifacts should be pruned.  This is the default.')
@click.option('-v', '--verbose', 'verbosity', count=True,
              help='Increases the verbosity of the logging.')
@click.option('--source-info-only', is_flag=True,
              help='Instead of building only updates the source infos.  The '
              'source info is used by the web admin panel to quickly find '
              'information about the source files (for instance jump to '
              'files).')
@click.option('--profile', is_flag=True,
              help='Enable build profiler.')
@pass_context
def build_cmd(ctx, output_path, watch, prune, verbosity,
              source_info_only, profile):
    """Builds the entire project into the final artifacts.

    The default behavior is to build the project into the default build
    output path which can be discovered with the `project-info` command
    but an alternative output folder can be provided with the `--output-path`
    option.

    The default behavior is to perform a build followed by a pruning step
    which removes no longer referenced artifacts from the output folder.
    Lektor will only build the files that require rebuilding if the output
    folder is reused.

    To enforce a clean build you have to issue a `clean` command first.
    """
    from lektor.builder import Builder
    from lektor.reporter import CliReporter

    if output_path is None:
        output_path = ctx.get_default_output_path()

    ctx.load_plugins()

    env = ctx.get_env()

    def _build():
        builder = Builder(env.new_pad(), output_path)
        if source_info_only:
            builder.update_all_source_infos()
        else:
            if profile:
                from .utils import profile_func
                profile_func(builder.build_all)
            else:
                builder.build_all()
            if prune:
                builder.prune()

    reporter = CliReporter(env, verbosity=verbosity)
    with reporter:
        _build()
        if not watch:
            return

        from lektor.watcher import watch
        click.secho('Watching for file system changes', fg='cyan')
        last_build = time.time()
        for ts, _, _ in watch(env):
            if ts > last_build:
                _build()
                last_build = time.time()


@cli.command('clean')
@click.option('-O', '--output-path', type=click.Path(), default=None,
              help='The output path.')
@click.option('-v', '--verbose', 'verbosity', count=True,
              help='Increases the verbosity of the logging.')
@click.confirmation_option(help='Confirms the cleaning.')
@pass_context
def clean_cmd(ctx, output_path, verbosity):
    """Cleans the entire build folder.

    If not build folder is provided, the default build folder of the project
    in the Lektor cache is used.
    """
    from lektor.builder import Builder
    from lektor.reporter import CliReporter

    if output_path is None:
        output_path = ctx.get_default_output_path()

    env = ctx.get_env()

    reporter = CliReporter(env, verbosity=verbosity)
    with reporter:
        builder = Builder(env.new_pad(), output_path)
        builder.prune(all=True)


@cli.command('deploy', short_help='Deploy the website.')
@click.argument('server', default='staging')
@click.option('-O', '--output-path', type=click.Path(), default=None,
              help='The output path.')
@pass_context
def deploy_cmd(ctx, server, output_path):
    """This command deploys the entire contents of the build folder
    (`--output-path`) onto a configured remote server.  The name of the
    server must fit the name from a target in the project configuration.
    """
    from lektor.publisher import publish

    if output_path is None:
        output_path = ctx.get_default_output_path()

    ctx.load_plugins()
    env = ctx.get_env()
    config = env.load_config()

    server_info = config.get_server(server)
    if server_info is None:
        raise click.BadParameter('Server "%s" does not exist.' % server,
                                 param_hint='server')

    event_iter = publish(env, server_info.target, output_path)
    if event_iter is None:
        raise click.UsageError('Server "%s" is not configured for a valid '
                               'publishing method.' % server)

    click.echo('Deploying to %s' % server_info.name)
    click.echo('  Build cache: %s' % output_path)
    click.echo('  Target: %s' % secure_url(server_info.target))
    for line in event_iter:
        click.echo('  %s' % click.style(line, fg='cyan'))
    click.echo('Done!')


@cli.command('server', short_help='Launch a local server.')
@click.option('-h', '--host', default='127.0.0.1',
              help='The network interface to bind to.  The default is the '
              'loopback device, but by setting it to 0.0.0.0 it becomes '
              'available on all network interfaces.')
@click.option('-p', '--port', default=5000, help='The port to bind to.',
              show_default=True)
@click.option('-O', '--output-path', type=click.Path(), default=None,
              help='The dev server will build into the same folder as '
              'the build command by default.')
@click.option('-v', '--verbose', 'verbosity', count=True,
              help='Increases the verbosity of the logging.')
@click.option('--browse', is_flag=True)
@pass_context
def server_cmd(ctx, host, port, output_path, verbosity, browse):
    """The server command will launch a local server for development.

    Lektor's developemnt server will automatically build all files into
    pages similar to how the build command with the `--watch` switch
    works, but also at the same time serve up the website on a local
    HTTP server.
    """
    from lektor.devserver import run_server
    if output_path is None:
        output_path = ctx.get_default_output_path()
    ctx.load_plugins()
    click.echo(' * Project path: %s' % ctx.get_project().project_path)
    click.echo(' * Output path: %s' % output_path)
    run_server((host, port), env=ctx.get_env(), output_path=output_path,
               verbosity=verbosity, ui_lang=ctx.ui_lang,
               lektor_dev=os.environ.get('LEKTOR_DEV') == '1',
               browse=browse)


@cli.command('project-info', short_help='Shows the info about a project.')
@click.option('as_json', '--json', is_flag=True,
              help='Prints out the data as json.')
@click.option('ops', '--name', is_flag=True, multiple=True,
              flag_value='name', help='Print the project name')
@click.option('ops', '--project-file', is_flag=True, multiple=True,
              flag_value='project_file',
              help='Print the path to the project file.')
@click.option('ops', '--tree', is_flag=True, multiple=True,
              flag_value='tree', help='Print the path to the tree.')
@click.option('ops', '--output-path', is_flag=True, multiple=True,
              flag_value='default_output_path',
              help='Print the path to the default output path.')
@pass_context
def project_info_cmd(ctx, as_json, ops):
    """Prints out information about the project.  This is particular
    useful for script usage or for discovering information about a
    Lektor project that is not immediately obvious (like the paths
    to the default output folder).
    """
    project = ctx.get_project()
    if as_json:
        echo_json(project.to_json())
        return

    if ops:
        data = project.to_json()
        for op in ops:
            click.echo(data.get(op, ''))
    else:
        click.echo('Name: %s' % project.name)
        click.echo('File: %s' % project.project_file)
        click.echo('Tree: %s' % project.tree)
        click.echo('Output: %s' % project.get_output_path())


@cli.command('content-file-info', short_help='Provides information for '
             'a set of lektor files.')
@click.option('as_json', '--json', is_flag=True,
              help='Prints out the data as json.')
@click.argument('files', nargs=-1, type=click.Path())
@pass_context
def content_file_info_cmd(ctx, files, as_json):
    """Given a list of files this returns the information for those files
    in the context of a project.  If the files are from different projects
    an error is generated.
    """
    project = None

    def fail(msg):
        if as_json:
            echo_json({'success': False, 'error': msg})
            sys.exit(1)
        raise click.UsageError('Could not find content file info: %s' % msg)

    for filename in files:
        this_project = Project.discover(filename)
        if this_project is None:
            fail('no project found')
        if project is None:
            project = this_project
        elif project.project_path != this_project.project_path:
            fail('multiple projects')

    if project is None:
        fail('no file indicated a project')

    project_files = []
    for filename in files:
        content_path = project.content_path_from_filename(filename)
        if content_path is not None:
            project_files.append(content_path)

    if not project_files:
        fail('no files resolve in project')

    if as_json:
        echo_json({
            'success': True,
            'project': project.to_json(),
            'paths': project_files,
        })
    else:
        click.echo('Project:')
        click.echo('  Name: %s' % project.name)
        click.echo('  File: %s' % project.project_file)
        click.echo('  Tree: %s' % project.tree)
        click.echo('Paths:')
        for project_file in project_files:
            click.echo('  - %s' % project_file)


@cli.command('plugins', short_help='Lists installed plugins.')
@click.option('as_json', '--json', is_flag=True,
              help='Prints out the data as json.')
@click.option('--reinstall', is_flag=True,
              help='Forces a fresh installation of the plugins.')
@click.option('--uninstall', is_flag=True,
              help='Forces an uninstallation of all plugins.')
@pass_context
def plugins_cmd(ctx, as_json, reinstall, uninstall):
    """Given a list of files this returns the information for those files
    in the context of a project.  If the files are from different projects
    an error is generated.
    """
    if uninstall:
        click.echo('Uninstalling all plugins ...')
        from .packages import wipe_package_cache
        wipe_package_cache(ctx.get_env())
        click.echo('All done!')
        return

    ctx.load_plugins(reinstall=reinstall)
    if reinstall:
        return

    env = ctx.get_env()
    plugins = sorted(env.plugins.values(), key=lambda x: x.id.lower())

    if as_json:
        echo_json({
            'plugins': [x.to_json() for x in plugins]
        })
        return

    for idx, plugin in enumerate(plugins):
        if idx:
            click.echo()
        click.echo('%s: %s' % (plugin.id, plugin.name))
        for line in plugin.description.splitlines():
            click.echo('  %s' % line)
        click.echo('  path: %s' % plugin.path)
        click.echo('  import-name: %s' % plugin.import_name)


@cli.command('quickstart', short_help='Starts a new empty project.')
@click.option('--name', help='The name of the project.')
@click.option('--path', type=click.Path(), help='Output directory')
@pass_context
def quickstart_cmd(ctx, **options):
    """Starts a new empty project with a minimum boilerplate."""
    from lektor.quickstart import project_quickstart
    project_quickstart(options)


from .devcli import cli as devcli
cli.add_command(devcli, 'dev')


main = cli
