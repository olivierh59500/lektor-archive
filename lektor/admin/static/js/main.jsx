'use strict';

var React = require('react');
var Router = require("react-router");
var {Route, DefaultRoute, NotFoundRoute} = Router;
var Component = require('./components/Component');
var i18n = require('./i18n');

require('bootstrap');
require('./bootstrap-extras');

// polyfill for internet explorer
require('native-promise-only');

// XXX: configurable!
i18n.currentLanguage = 'de';

class BadRoute extends Component {

  render() {
    return (
      <div>
        <h2>Nothing to see here</h2>
        <p>There is really nothing to see here.</p>
      </div>
    );
  }
}

var routes = (function() {
  // route targets
  var App = require('./views/App');
  var Dash = require('./views/Dash');
  var EditPage = require('./views/EditPage');
  var DeletePage = require('./views/DeletePage');
  var PreviewPage = require('./views/PreviewPage');
  var AddChildPage = require('./views/AddChildPage');
  var AddAttachmentPage = require('./views/AddAttachmentPage');

  // route setup
  return (
    <Route name="dash" path={$LEKTOR_CONFIG.admin_root} handler={App}>
      <Route name="edit" path=":path/edit" handler={EditPage}/>
      <Route name="delete" path=":path/delete" handler={DeletePage}/>
      <Route name="preview" path=":path/preview" handler={PreviewPage}/>
      <Route name="add-child" path=":path/add-child" handler={AddChildPage}/>
      <Route name="add-attachment" path=":path/upload" handler={AddAttachmentPage}/>
      <DefaultRoute handler={Dash}/>
      <NotFoundRoute handler={BadRoute}/>
    </Route>
  );
})();

var router = Router.create({
  routes: routes,
  location: Router.HistoryLocation,
  scrollBehavior: Router.ImitateBrowserBehavior
});

router.run(function(Handler) {
  React.render(<Handler/>, document.getElementById('dash'));
});
