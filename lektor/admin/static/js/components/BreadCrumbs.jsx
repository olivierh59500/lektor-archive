'use strict';

var React = require('react');
var Router = require("react-router");
var {Link, RouteHandler} = Router;

var RecordComponent = require('./RecordComponent');
var utils = require('../utils');
var i18n = require('../i18n');


class BreadCrumbs extends RecordComponent {

  constructor(props) {
    super(props);
    this.state = {
      recordPathInfo: null
    };
  }

  componentDidMount() {
    super();
    this.updateCrumbs();
  }

  componentWillReceiveProps(nextProps) {
    super(nextProps);
    this.updateCrumbs();
  }

  updateCrumbs() {
    var path = this.getRecordPath();
    if (path === null) {
      this.setState({
        recordPathInfo: null
      });
      return;
    }

    utils.loadData('/pathinfo', {path: path})
      .then((resp) => {
        this.setState({
          recordPathInfo: {
            path: path,
            segments: resp.segments
          }
        });
      });
  }

  onCloseClick(e) {
    var segs = this.state.recordPathInfo.segments;
    if (segs.length > 0) {
      window.location.href = utils.getCanonicalUrl(segs[segs.length - 1].url_path);
      e.preventDefault();
    }
  }

  render() {
    var crumbs = [];
    var target = this.isRecordPreviewActive() ? 'preview' : 'edit';
    var lastItem = null;

    if (this.state.recordPathInfo != null) {
      crumbs = this.state.recordPathInfo.segments.map((item) => {
        var urlPath = this.getUrlRecordPathWithAlt(item.path);
        var label = item.label;
        var className = 'record-crumb';

        if (!item.exists) {
          label = item.id;
          className += ' missing-record-crumb';
        }
        lastItem = item;

        return (
          <li key={item.path} className={className}>
            <Link to={target} params={{path: urlPath}}>{label}</Link>
          </li>
        );
      });
    } else {
      crumbs = (
        <li><Link to={'edit'} params={{path: 'root'}}>{i18n.trans('BACK_TO_OVERVIEW')}</Link></li>
      )
    }

    return (
      <div className="breadcrumbs">
        <ul className="breadcrumb container">
          {this.props.children}
          {crumbs}
          {lastItem && lastItem.can_have_children ? (
            <li className="new-record-crumb">
              <Link to="add-child" params={{path: this.getUrlRecordPathWithAlt(
                lastItem.path)}}>+</Link>
            </li>
          ) : null}
          {' ' /* this space is needed for chrome ... */}
          <li className="meta">
            <Link to="publish">{i18n.trans('PUBLISH')}</Link>
            <a href="/" onClick={
              this.onCloseClick.bind(this)}>{i18n.trans('RETURN_TO_WEBSITE')}</a>
          </li>
        </ul>
      </div>
    );
  }
}

module.exports = BreadCrumbs;
