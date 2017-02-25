# -*- coding: utf-8 -*-

# For debugging
# NVIM_PYTHON_LOG_FILE=nvim.log NVIM_PYTHON_LOG_LEVEL=INFO nvim

import os
import re
import logging
import copy
import importlib
import threading
from threading import Thread, RLock
import urllib
import json
from neovim import attach
from http.server import BaseHTTPRequestHandler, HTTPServer
import cm

logger = logging.getLogger(__name__)

# use a trick to only register the source withou loading the entire
# module
class CmSkipLoading(Exception):
    pass

class CoreHandler:

    def __init__(self,nvim):

        self._nvim = nvim

        # { '{source_name}': {'startcol': , 'matches'}
        self._matches = {}
        self._sources = {}
        self._last_startcol = 0
        self._last_matches = []
        # should be True for supporting display menu directly without cm_refresh
        self._has_popped_up = True
        self._subscope_detectors = {}

        scoper_paths = self._nvim.eval("globpath(&rtp,'pythonx/cm_scopers/*.py',1)").split("\n")

        # auto find scopers
        for path in scoper_paths:
            if not path:
                continue
            try:
                modulename = os.path.splitext(os.path.basename(path))[0]
                modulename = "cm_scopers.%s" % modulename
                m = importlib.import_module(modulename)

                scoper = m.Scoper()
                for scope in scoper.scopes:
                    if scope not in self._subscope_detectors:
                        self._subscope_detectors[scope] = []
                    self._subscope_detectors[scope].append(scoper)
                    logger.info('scoper <%s> imported for %s', modulename, scope)


            except Exception as ex:
                logger.exception('importing scoper <%s> failed: %s', modulename, ex)

        # auto find sources
        sources_paths = self._nvim.eval("globpath(&rtp,'pythonx/cm_sources/*.py',1)").split("\n")
        for path in sources_paths:

            modulename = os.path.splitext(os.path.basename(path))[0]
            modulename = "cm_sources.%s" % modulename

            # use a trick to only register the source withou loading the entire
            # module
            def register_source(name,abbreviation,priority,enable=True,events=[],detach=0,python='python3',**kwargs):

                channel = dict(type=python,
                               module=modulename,
                               detach=detach,
                               events=events)

                source = {}
                source['channel']      = channel
                source['name']         = name
                source['priority']     = priority
                source['enable']       = enable
                source['abbreviation'] = abbreviation
                source['enable']       = enable
                for k in kwargs:
                    source[k] = kwargs[k]

                logger.info('registering source: %s',source)
                nvim.call('cm#register_source',source)

                # use a trick to only register the source withou loading the entire
                # module
                raise CmSkipLoading()

            cm.register_source = register_source
            try:
                # register_source
                m = importlib.import_module(modulename)
            except CmSkipLoading:
                # This is not an error
                logger.info('source <%s> registered', modulename)
            except Exception as ex:
                logger.exception("register_source for %s failed", modulename)


        logger.info('_subscope_detectors: %s', self._subscope_detectors)

        self._ctx = None

    def _is_kw_futher_typing(self,oldctx,curctx):
        old_typed = oldctx['typed']
        cur_typed = curctx['typed']

        old_len = len(old_typed)
        cur_len = len(cur_typed)

        if cur_len < old_len:
            return False

        if cur_typed[0:old_len] != old_typed:
            return False

        if re.match(r'^[a-zA-Z0-9_]*$',cur_typed[old_len:]):
            return True

        return False

    def cm_complete(self,srcs,name,ctx,startcol,matches,refresh,outdated,current_ctx):

        if isinstance(name,dict):
            name = name['name']

        if name not in srcs:
            logger.error("invalid completion source name [%s]", name)
            return

        # be careful when completion matches context is outdated
        if outdated:
            logger.info("[%s] outdated matches, old typed [%s] cur typed[%s]", name, ctx['typed'], current_ctx['typed'])
            if refresh or not self._is_kw_futher_typing(ctx,current_ctx):
                logger.info("[%s] matches is outdated. ignore them.", name)
                return
            logger.info("[%s] matches is outdated by keyword futher typing. I'm gonna keep it.", name)

        # adjust for subscope
        if ctx['lnum']==1:
            startcol += ctx.get('scope_col',1)-1

        self._sources = srcs

        try:

            # process the matches early to eliminate unnecessary complete function call
            result = self.process_matches(name,ctx,startcol,matches)

            if (not result) and (not self._matches.get(name,{}).get('last_matches',[])):
                # not popping up, ignore this request
                logger.debug('Not popping up, not refreshing for cm_complete by %s, startcol %s', name, startcol)
                return

        finally:

            # storing matches

            if name not in self._matches:
                self._matches[name] = {}

            if len(matches)==0:
                del self._matches[name]
            else:
                self._matches[name]['startcol'] = startcol
                self._matches[name]['refresh'] = refresh
                self._matches[name]['matches'] = matches

        # wait for cm_complete_timeout, reduce flashes
        if self._has_popped_up:
            logger.info("update popup for [%s]",name)
            # the ctx in parameter maybe a subctx for completion source, use
            # nvim.call to get the root context
            self._refresh_completions(self._nvim.call('cm#context'))
        else:
            logger.debug("delay popup for [%s]",name)

    def cm_insert_enter(self):
        self._matches = {}
        self._last_matches = []
        self._last_startcol = 0

    def cm_complete_timeout(self,srcs,ctx,*args):
        if not self._has_popped_up:
            self._refresh_completions(ctx)
            self._has_popped_up = True

    # The completion core itself
    def cm_refresh(self,srcs,root_ctx,*args):

        # update file server
        self._ctx = root_ctx

        # initial scope
        root_ctx['scope'] = root_ctx['filetype']

        self._sources = srcs
        self._has_popped_up = False

        # simple complete done
        if root_ctx['typed'] == '':
            self._matches = {}
        elif re.match(r'[^0-9a-zA-Z_]',root_ctx['typed'][-1]):
            self._matches = {}

        ctx_lists = [root_ctx,]

        # scoping
        i = 0
        while i<len(ctx_lists):
            ctx = ctx_lists[i]
            scope = ctx['scope']
            if scope in self._subscope_detectors:
                for detector in self._subscope_detectors[scope]:
                    try:
                        sub_ctx = detector.sub_context(ctx, cm.get_src(self._nvim,ctx))
                        if sub_ctx:
                            # adjust offset to global based and add the new
                            # context
                            sub_ctx['scope_offset'] += ctx.get('scope_offset',0)
                            sub_ctx['scope_lnum'] += ctx.get('scope_lnum',1)-1
                            if int(sub_ctx['lnum']) == 1:
                                sub_ctx['typed'] = sub_ctx['typed'][sub_ctx['scope_col']-1:]
                                sub_ctx['scope_col'] += ctx.get('scope_col',1)-1
                                logger.info('adjusting scope_col')
                            ctx_lists.append(sub_ctx)
                            logger.info('new sub context: %s', sub_ctx)
                    except Exception as ex:
                        logger.exception("exception on scope processing: %s", ex)

            i += 1

        # do notify_sources_to_refresh
        refreshes_calls = []
        refreshes_channels = []

        # get the sources that need to be notified
        for ctx in ctx_lists:
            for name in srcs:

                info = srcs[name]
                if not info.get('enable',True):
                    # ignore disabled source
                    continue

                try:

                    if not self._check_scope(ctx,info):
                        logger.debug('_check_scope ignore <%s> for context scope <%s>', name, ctx['scope'])
                        continue

                    if (name in self._matches) and not self._matches[name]['refresh']:
                        # no need to refresh
                        logger.debug('cached for <%s>, no need to refresh', name)
                        continue

                    if not self._check_refresh_patterns(ctx['typed'],info):
                        continue

                    if 'cm_refresh' in info:
                        # check patterns when necessary
                        refreshes_calls.append(dict(name=name,context=ctx))

                    # start channels on demand here
                    if 'channel' in info:
                        channel = info['channel']
                        if 'id' not in channel:
                            if channel.get('has_terminated',0)==0:
                                logger.info('starting channels for %s',name)
                                # has not been started yet, start it now
                                self._nvim.call('cm#_start_channel',name,async=True)

                    channel = info.get('channel',{})
                    if 'id' in channel:
                        refreshes_channels.append(dict(name=name,id=channel['id'],context=ctx))
                except Exception as inst:
                    logger.exception('cm_refresh process exception: %s', inst)
                    continue

        if not refreshes_calls and not refreshes_channels:
            logger.info('not notifying any channels, _refresh_completions now')
            self._refresh_completions(root_ctx)
            self._has_popped_up = True
        else:
            logger.info('notify_sources_to_refresh calls cnt [%s], channels cnt [%s]',len(refreshes_calls),len(refreshes_channels))
            logger.debug('cm#_notify_sources_to_refresh [%s] [%s] [%s]', refreshes_calls, refreshes_channels, root_ctx)
            self._nvim.call('cm#_notify_sources_to_refresh', refreshes_calls, refreshes_channels, root_ctx)

    # check patterns for dict, if non dict, return True
    def _check_refresh_patterns(self,typed,opt):
        if type(opt)!=type({}):
            return True
        patterns = opt.get('cm_refresh_patterns',None)
        if not patterns:
            return True
        for pattern in patterns:
            if re.search(pattern,typed):
                return True
        return False

    # almost the same as `s:check_scope` in `autoload/cm.vim`
    def _check_scope(self,ctx,info):
        scopes = info.get('scopes',None)
        cur_scope = ctx.get('scope',ctx['filetype'])
        is_root_scope = ( cur_scope==ctx['filetype'] )
        if not scopes:
            # scopes setting is None, means that this is a general purpose
            # completion source, only complete for the root scope
            if is_root_scope:
                return True
            else:
                return False
        for scope in scopes:
            if scope==cur_scope:
                if info.get('scoping',False):
                    return True
                else:
                    return is_root_scope
        return False

    def _refresh_completions(self,ctx):

        matches = []

        # sort by priority
        names = sorted(self._matches.keys(),key=lambda x: self._sources[x]['priority'], reverse=True)

        if len(names)==0:
            # empty
            logger.info('_refresh_completions names: %s, startcol: %s, matches: %s', names, ctx['col'], [])
            self._complete(ctx, ctx['col'], [])
            return

        col = ctx['col']
        startcol = col

        # basick processing per source
        for name in names:

            try:

                self._matches[name]['last_matches'] = []

                source_startcol = self._matches[name]['startcol']
                if source_startcol>col or source_startcol==0:
                    self._matches[name]['last_matches'] = []
                    logger.error('ignoring invalid startcol for %s %s', name, self._matches[name]['startcol'])
                    continue

                source_matches = self._matches[name]['matches']
                source_matches = self.process_matches(name,ctx,source_startcol,source_matches)

                self._matches[name]['last_matches'] = source_matches

                if not source_matches:
                    continue

                # min non empty source_matches's source_startcol as startcol
                if source_startcol < startcol:
                    startcol = source_startcol

            except Exception as inst:
                logger.exception('_refresh_completions process exception: %s', inst)
                continue

        # merge processing results of sources
        for name in names:

            try:
                source_startcol = self._matches[name]['startcol']
                source_matches = self._matches[name]['last_matches']
                if not source_matches:
                    continue

                prefix = ctx['typed'][startcol-1 : source_startcol-1]

                for e in source_matches:
                    e['word'] = prefix + e['word']
                    # if 'abbr' in e:
                    #     e['abbr'] = prefix + e['abbr']

                matches += source_matches

            except Exception as inst:
                logger.exception('_refresh_completions process exception: %s', inst)
                continue

        if not matches:
            startcol=len(ctx['typed']) or 1
        logger.info('_refresh_completions names: %s, startcol: %s, matches cnt: %s', names, startcol, len(matches))
        logger.debug('_refresh_completions names: %s, startcol: %s, matches: %s, source matches: %s', names, startcol, matches, self._matches)
        self._complete(ctx, startcol, matches)

    def process_matches(self,name,ctx,startcol,matches):

        abbr = self._sources[name].get('abbreviation','')

        # formalize datastructure
        formalized = []
        for item in matches:
            e = {}
            if type(item)==type(''):
                e['word'] = item
            else:
                e = copy.deepcopy(item)
            formalized.append(e)

        # filtering and sorting
        result = cm.get_matcher(self._nvim).process(name,ctx,startcol,formalized)

        # fix some text
        for e in result:

            if 'menu' not in e:
                if 'info' in e and e['info'] and len(e['info'])<50:
                    if abbr:
                        e['menu'] = "<%s> %s" % (abbr,e['info'])
                    else:
                        e['menu'] = e['info']
                else:
                    # info too long
                    if abbr:
                        e['menu'] = "<%s>" % abbr
            else:
                # e['menu'] = "<%s> %s"  % (self._sources[name]['abbreviation'], e['info'])
                pass

        return result


    def _complete(self, ctx, startcol, matches):
        if not matches and not self._last_matches:
            # no need to fire complete message
            logger.info('matches==0, _last_matches==0, ignore')
            return
        if self._last_startcol==startcol and self._last_matches==matches:
            logger.info('ignore _complete call: self._last_startcol==startcol and self._last_matches==matches')
            return
        self._nvim.call('cm#_core_complete', ctx, startcol, matches, async=True)
        self._last_matches = matches
        self._last_startcol = startcol
