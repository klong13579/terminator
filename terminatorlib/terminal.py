#!/usr/bin/python
# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""terminal.py - classes necessary to provide Terminal widgets"""

import sys
import os
import pygtk
pygtk.require('2.0')
import gtk
import gobject
import pango
import subprocess
import urllib

from util import dbg, err, gerr, get_top_window
import util
from config import Config
from cwd import get_default_cwd
from terminator import Terminator
from titlebar import Titlebar
from terminal_popup_menu import TerminalPopupMenu
from searchbar import Searchbar
from translation import _
from signalman import Signalman
import plugin

try:
    import vte
except ImportError:
    gerr('You need to install python bindings for libvte')
    sys.exit(1)

# pylint: disable-msg=R0904
class Terminal(gtk.VBox):
    """Class implementing the VTE widget and its wrappings"""

    __gsignals__ = {
        'close-term': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'title-change': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
            (gobject.TYPE_STRING,)),
        'enumerate': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
            (gobject.TYPE_INT,)),
        'group-tab': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'ungroup-tab': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'ungroup-all': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'split-horiz': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'split-vert': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'tab-new': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'tab-top-new': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'focus-in': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'zoom': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'maximise': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'unzoom': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'resize-term': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
            (gobject.TYPE_STRING,)),
        'navigate': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
            (gobject.TYPE_STRING,)),
        'tab-change': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE,
            (gobject.TYPE_INT,)),
        'group-all': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'ungroup-all': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'group-tab': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
        'ungroup-tab': (gobject.SIGNAL_RUN_LAST, gobject.TYPE_NONE, ()),
    }

    TARGET_TYPE_VTE = 8

    terminator = None
    vte = None
    terminalbox = None
    scrollbar = None
    scrollbar_position = None
    titlebar = None
    searchbar = None

    group = None
    cwd = None
    command = None
    clipboard = None
    pid = None

    matches = None
    config = None
    default_encoding = None
    custom_encoding = None
    custom_font_size = None

    composite_support = None

    cnxids = None

    def __init__(self):
        """Class initialiser"""
        gtk.VBox.__init__(self)
        self.__gobject_init__()

        self.terminator = Terminator()
        self.terminator.register_terminal(self)

        self.connect('enumerate', self.terminator.do_enumerate)
        self.connect('group-tab', self.terminator.group_tab)
        self.connect('ungroup-tab', self.terminator.ungroup_tab)
        self.connect('focus-in', self.terminator.focus_changed)

        self.matches = {}
        self.cnxids = Signalman()

        self.config = Config()

        self.cwd = get_default_cwd()
        self.clipboard = gtk.clipboard_get(gtk.gdk.SELECTION_CLIPBOARD)

        self.vte = vte.Terminal()
        self.vte.set_size(80, 24)
        self.vte._expose_data = None
        if not hasattr(self.vte, "set_opacity") or \
           not hasattr(self.vte, "is_composited"):
            self.composite_support = False
        self.vte.show()

        self.default_encoding = self.vte.get_encoding()
        self.update_url_matches(self.config['try_posix_regexp'])

        self.terminalbox = self.create_terminalbox()

        self.titlebar = Titlebar(self)
        self.titlebar.connect_icon(self.on_group_button_press)
        self.titlebar.connect('edit-done', self.on_edit_done)
        self.connect('title-change', self.titlebar.set_terminal_title)
        self.titlebar.connect('create-group', self.really_create_group)

        self.searchbar = Searchbar()
        self.searchbar.connect('end-search', self.on_search_done)

        self.show()
        self.pack_start(self.titlebar, False)
        self.pack_start(self.terminalbox)
        self.pack_end(self.searchbar)

        self.connect_signals()

        os.putenv('COLORTERM', 'gnome-terminal')

        env_proxy = os.getenv('http_proxy')
        if not env_proxy:
            if self.config['http_proxy'] and self.config['http_proxy'] != '':
                os.putenv('http_proxy', self.config['http_proxy'])

    def set_profile(self, widget, profile):
        """Set our profile"""
        if profile != self.config.get_profile():
            self.config.set_profile(profile)
            self.reconfigure()

    def get_profile(self):
        """Return our profile name"""
        return(self.config.profile)

    def close(self):
        """Close ourselves"""
        dbg('Terminal::close: emitting close-term')
        self.emit('close-term')

    def create_terminalbox(self):
        """Create a GtkHBox containing the terminal and a scrollbar"""

        terminalbox = gtk.HBox()
        self.scrollbar = gtk.VScrollbar(self.vte.get_adjustment())
        self.scrollbar.set_no_show_all(True)
        self.scrollbar_position = self.config['scrollbar_position']

        if self.scrollbar_position not in ('hidden', 'disabled'):
            self.scrollbar.show()

        if self.scrollbar_position == 'left':
            func = terminalbox.pack_end
        else:
            func = terminalbox.pack_start

        func(self.vte)
        func(self.scrollbar, False)
        terminalbox.show()

        return(terminalbox)

    def update_url_matches(self, posix = True):
        """Update the regexps used to match URLs"""
        userchars = "-A-Za-z0-9"
        passchars = "-A-Za-z0-9,?;.:/!%$^*&~\"#'"
        hostchars = "-A-Za-z0-9"
        pathchars = "-A-Za-z0-9_$.+!*(),;:@&=?/~#%'\""
        schemes   = "(news:|telnet:|nntp:|file:/|https?:|ftps?:|webcal:)"
        user      = "[" + userchars + "]+(:[" + passchars + "]+)?"
        urlpath   = "/[" + pathchars + "]*[^]'.}>) \t\r\n,\\\"]"

        if posix:
            dbg ('Terminal::update_url_matches: Trying POSIX URL regexps')
            lboundry = "[[:<:]]"
            rboundry = "[[:>:]]"
        else: # GNU
            dbg ('Terminal::update_url_matches: Trying GNU URL regexps')
            lboundry = "\\<"
            rboundry = "\\>"

        self.matches['full_uri'] = self.vte.match_add(lboundry + schemes + 
                "//(" + user + "@)?[" + hostchars  +".]+(:[0-9]+)?(" + 
                urlpath + ")?" + rboundry + "/?")

        if self.matches['full_uri'] == -1:
            if posix:
                err ('Terminal::update_url_matches: POSIX failed, trying GNU')
                self.update_url_matches(posix = False)
            else:
                err ('Terminal::update_url_matches: Failed adding URL matches')
        else:
            self.matches['voip'] = self.vte.match_add(lboundry + 
                    '(callto:|h323:|sip:)' + "[" + userchars + "+][" + 
                    userchars + ".]*(:[0-9]+)?@?[" + pathchars + "]+" + 
                    rboundry)
            self.matches['addr_only'] = self.vte.match_add (lboundry + 
                    "(www|ftp)[" + hostchars + "]*\.[" + hostchars + 
                    ".]+(:[0-9]+)?(" + urlpath + ")?" + rboundry + "/?")
            self.matches['email'] = self.vte.match_add (lboundry + 
                    "(mailto:)?[a-zA-Z0-9][a-zA-Z0-9.+-]*@[a-zA-Z0-9]" +
                            "[a-zA-Z0-9-]*\.[a-zA-Z0-9][a-zA-Z0-9-]+" +
                            "[.a-zA-Z0-9-]*" + rboundry)
            self.matches['nntp'] = self.vte.match_add (lboundry + 
                  """news:[-A-Z\^_a-z{|}~!"#$%&'()*+,./0-9;:=?`]+@""" +
                            "[-A-Za-z0-9.]+(:[0-9]+)?" + rboundry)

            # Now add any matches from plugins
            try:
                registry = plugin.PluginRegistry()
                registry.load_plugins()
                plugins = registry.get_plugins_by_capability('url_handler')

                for urlplugin in plugins:
                    name = urlplugin.handler_name
                    match = urlplugin.match
                    self.matches[name] = self.vte.match_add(match)
                    dbg('Terminal::update_matches: added plugin URL handler \
for %s (%s)' % (name, urlplugin.__class__.__name__))
            except Exception, ex:
                err('Terminal::update_url_matches: %s' % ex)
            
    def connect_signals(self):
        """Connect all the gtk signals and drag-n-drop mechanics"""

        self.vte.connect('key-press-event', self.on_keypress)
        self.vte.connect('button-press-event', self.on_buttonpress)
        self.vte.connect('popup-menu', self.popup_menu)

        srcvtetargets = [("vte", gtk.TARGET_SAME_APP, self.TARGET_TYPE_VTE)]
        dsttargets = [("vte", gtk.TARGET_SAME_APP, self.TARGET_TYPE_VTE), 
                ('text/plain', 0, 0), ('STRING', 0, 0), ('COMPOUND_TEXT', 0, 0)]

        for (widget, mask) in [
            (self.vte, gtk.gdk.CONTROL_MASK | gtk.gdk.BUTTON3_MASK), 
            (self.titlebar, gtk.gdk.BUTTON1_MASK)]:
            widget.drag_source_set(mask, srcvtetargets, gtk.gdk.ACTION_MOVE)

        self.vte.drag_dest_set(gtk.DEST_DEFAULT_MOTION |
                gtk.DEST_DEFAULT_HIGHLIGHT | gtk.DEST_DEFAULT_DROP,
                dsttargets, gtk.gdk.ACTION_MOVE)

        for widget in [self.vte, self.titlebar]:
            widget.connect('drag-begin', self.on_drag_begin, self)
            widget.connect('drag-data-get', self.on_drag_data_get,
            self)

        self.vte.connect('drag-motion', self.on_drag_motion, self)
        self.vte.connect('drag-data-received',
            self.on_drag_data_received, self)

        # FIXME: Shouldn't this be in configure()?
        if self.config['copy_on_selection']:
            self.cnxids.new(self.vte, 'selection-changed', 
                    lambda widget: self.vte.copy_clipboard())

        if self.composite_support:
            self.vte.connect('composited-changed',
                self.on_composited_changed)

        self.vte.connect('window-title-changed', lambda x:
            self.emit('title-change', self.get_window_title()))
        self.vte.connect('grab-focus', self.on_vte_focus)
        self.vte.connect('focus-in-event', self.on_vte_focus_in)
        self.vte.connect('size-allocate', self.on_vte_size_allocate)

        self.vte.add_events(gtk.gdk.ENTER_NOTIFY_MASK)
        self.vte.connect('enter_notify_event',
            self.on_vte_notify_enter)

        self.cnxids.new(self.vte, 'realize', self.reconfigure)

    def create_popup_group_menu(self, widget, event = None):
        """Pop up a menu for the group widget"""
        if event:
            button = event.button
            time = event.time
        else:
            button = 0
            time = 0

        menu = self.populate_group_menu()
        menu.show_all()
        menu.popup(None, None, self.position_popup_group_menu, button, time,
                widget)
        return(True)

    def populate_group_menu(self):
        """Fill out a group menu"""
        menu = gtk.Menu()
        groupitem = None

        item = gtk.MenuItem(_('New group...'))
        item.connect('activate', self.create_group)
        menu.append(item)

        if len(self.terminator.groups) > 0:
            groupitem = gtk.RadioMenuItem(groupitem, _('None'))
            groupitem.set_active(self.group == None)
            groupitem.connect('activate', self.set_group, None)
            menu.append(groupitem)

            for group in self.terminator.groups:
                item = gtk.RadioMenuItem(groupitem, group, False)
                item.set_active(self.group == group)
                item.connect('toggled', self.set_group, group)
                menu.append(item)
                groupitem = item

        if self.group != None or len(self.terminator.groups) > 0:
            menu.append(gtk.MenuItem())

        if self.group != None:
            item = gtk.MenuItem(_('Remove group %s') % self.group)
            item.connect('activate', self.ungroup, self.group)
            menu.append(item)

        if util.has_ancestor(self, gtk.Notebook):
            item = gtk.MenuItem(_('G_roup all in tab'))
            item.connect('activate', lambda x: self.emit('group_tab'))
            menu.append(item)

            if len(self.terminator.groups) > 0:
                item = gtk.MenuItem(_('Ungr_oup all in tab'))
                item.connect('activate', lambda x: self.emit('ungroup_tab'))
                menu.append(item)

        if len(self.terminator.groups) > 0:
            item = gtk.MenuItem(_('Remove all groups'))
            item.connect('activate', lambda x: self.emit('ungroup-all'))
            menu.append(item)

        if self.group != None:
            menu.append(gtk.MenuItem())

            item = gtk.MenuItem(_('Close group %s') % self.group)
            item.connect('activate', lambda x:
                         self.terminator.closegroupedterms(self.group))
            menu.append(item)

        menu.append(gtk.MenuItem())

        groupitem = None

        for key, value in {_('Broadcast all'):'all', 
                          _('Broadcast group'):'group',
                          _('Broadcast off'):'off'}.items():
            groupitem = gtk.RadioMenuItem(groupitem, key)
            dbg('Terminal::populate_group_menu: %s active: %s' %
                    (key, self.terminator.groupsend ==
                        self.terminator.groupsend_type[value]))
            groupitem.set_active(self.terminator.groupsend ==
                    self.terminator.groupsend_type[value])
            groupitem.connect('activate', self.set_groupsend,
                    self.terminator.groupsend_type[value])
            menu.append(groupitem)

        menu.append(gtk.MenuItem())

        item = gtk.CheckMenuItem(_('Split to this group'))
        item.set_active(self.config['split_to_group'])
        item.connect('toggled', lambda x: self.do_splittogroup_toggle())
        menu.append(item)

        item = gtk.CheckMenuItem(_('Autoclean groups'))
        item.set_active(self.config['autoclean_groups'])
        item.connect('toggled', lambda x: self.do_autocleangroups_toggle())
        menu.append(item)

        menu.append(gtk.MenuItem())

        item = gtk.MenuItem(_('Insert terminal number'))
        item.connect('activate', lambda x: self.emit('enumerate', False))
        menu.append(item)

        item = gtk.MenuItem(_('Insert padded terminal number'))
        item.connect('activate', lambda x: self.emit('enumerate', True))
        menu.append(item)

        return(menu)

    def position_popup_group_menu(self, menu, widget):
        """Calculate the position of the group popup menu"""
        screen_w = gtk.gdk.screen_width()
        screen_h = gtk.gdk.screen_height()

        widget_win = widget.get_window()
        widget_x, widget_y = widget_win.get_origin()
        widget_w, widget_h = widget_win.get_size()

        menu_w, menu_h = menu.size_request()

        if widget_y + widget_h + menu_h > screen_h:
            menu_y = max(widget_y - menu_h, 0)
        else:
            menu_y = widget_y + widget_h

        return(widget_x, menu_y, 1)

    def set_group(self, item, name):
        """Set a particular group"""
        if self.group == name:
            # already in this group, no action needed
            return
        dbg('Terminal::set_group: Setting group to %s' % name)
        self.group = name
        self.titlebar.set_group_label(name)
        self.terminator.group_hoover()

    def create_group(self, item):
        """Trigger the creation of a group via the titlebar (because popup 
        windows are really lame)"""
        self.titlebar.create_group()

    def really_create_group(self, widget, groupname):
        """The titlebar has spoken, let a group be created"""
        self.terminator.create_group(groupname)
        self.set_group(None, groupname)

    def ungroup(self, widget, data):
        """Remove a group"""
        # FIXME: Could we emit and have Terminator do this?
        for term in self.terminator.terminals:
            if term.group == data:
                term.set_group(None, None)
        self.terminator.group_hoover()

    def set_groupsend(self, widget, value):
        """Set the groupsend mode"""
        # FIXME: Can we think of a smarter way of doing this than poking?
        if value in self.terminator.groupsend_type.values():
            dbg('Terminal::set_groupsend: setting groupsend to %s' % value)
            self.terminator.groupsend = value

    def do_splittogroup_toggle(self):
        """Toggle the splittogroup mode"""
        self.config['split_to_group'] = not self.config['split_to_group']

    def do_autocleangroups_toggle(self):
        """Toggle the autocleangroups mode"""
        self.config['autoclean_groups'] = not self.config['autoclean_groups']

    def reconfigure(self, widget=None):
        """Reconfigure our settings"""
        dbg('Terminal::reconfigure')
        self.cnxids.remove_signal(self.vte, 'realize')

        # Handle child command exiting
        self.cnxids.remove_signal(self.vte, 'child-exited')

        if self.config['exit_action'] == 'restart':
            self.cnxids.new(self.vte, 'child-exited', self.spawn_child)
        elif self.config['exit_action'] in ('close', 'left'):
            self.cnxids.new(self.vte, 'child-exited', 
                                            lambda x: self.emit('close-term'))

        self.vte.set_emulation(self.config['emulation'])
        if self.custom_encoding != True:
            self.vte.set_encoding(self.config['encoding'])
        self.vte.set_word_chars(self.config['word_chars'])
        self.vte.set_mouse_autohide(self.config['mouse_autohide'])

        backspace = self.config['backspace_binding']
        delete = self.config['delete_binding']

        # FIXME: This doesn't seem like we ever obey control-h or
        # escape-sequence
        try:
            if backspace == 'ascii-del':
                backbind = vte.ERASE_ASCII_BACKSPACE
            else:
                backbind = vte.ERASE_AUTO_BACKSPACE
        except AttributeError:
            if backspace == 'ascii-del':
                backbind = 2
            else:
                backbind = 1

        try:
            if delete == 'escape-sequence':
                delbind = vte.ERASE_DELETE_SEQUENCE
            else:
                delbind = vte.ERASE_AUTO
        except AttributeError:
            if delete == 'escape-sequence':
                delbind = 3
            else:
                delbind = 0

        self.vte.set_backspace_binding(backbind)
        self.vte.set_delete_binding(delbind)

        if not self.custom_font_size:
            try:
                self.vte.set_font(pango.FontDescription(self.config['font']))
            except:
                pass
        self.vte.set_allow_bold(self.config['allow_bold'])
        if self.config['use_theme_colors']:
            fgcolor = self.vte.get_style().text[gtk.STATE_NORMAL]
            bgcolor = self.vte.get_style().base[gtk.STATE_NORMAL]
        else:
            fgcolor = gtk.gdk.color_parse(self.config['foreground_color'])
            bgcolor = gtk.gdk.color_parse(self.config['background_color'])

        colors = self.config['palette'].split(':')
        palette = []
        for color in colors:
            if color:
                palette.append(gtk.gdk.color_parse(color))
        self.vte.set_colors(fgcolor, bgcolor, palette)
        if self.config['cursor_color'] != '':
            self.vte.set_color_cursor(gtk.gdk.color_parse(self.config['cursor_color']))
        if hasattr(self.vte, 'set_cursor_shape'):
            self.vte.set_cursor_shape(getattr(vte, 'CURSOR_SHAPE_' +
                self.config['cursor_shape'].upper()))

        background_type = self.config['background_type']
        if background_type == 'image' and \
           self.config['background_image'] is not None and \
           self.config['background_image'] != '':
            self.vte.set_background_image_file(self.config['background_image'])
            self.vte.set_scroll_background(self.config['scroll_background'])
        else:
            self.vte.set_background_image_file('')
            self.vte.set_scroll_background(False)

        opacity = 65536
        if background_type in ('image', 'transparent'):
            self.vte.set_background_tint_color(gtk.gdk.color_parse(self.config['background_color']))
            self.vte.set_background_saturation(1 -
                    (self.config['background_darkness']))
            opacity = int(self.config['background_darkness'] * 65536)
        else:
            self.vte.set_background_saturation(1)

        if self.composite_support:
            self.vte.set_opacity(opacity)
        if self.config['background_type'] == 'transparent':
            self.vte.set_background_transparent(True)

        self.vte.set_cursor_blinks(self.config['cursor_blink'])

        if self.config['force_no_bell'] == True:
            self.vte.set_audible_bell(False)
            self.vte.set_visible_bell(False)
            self.cnxids.remove_signal(self.vte, 'beep')
        else:
            self.vte.set_audible_bell(self.config['audible_bell'])
            self.vte.set_visible_bell(self.config['visible_bell'])
            self.cnxids.remove_signal(self.vte, 'beep')
            if self.config['urgent_bell'] == True:
                try:
                    self.cnxids.new(self.vte, 'beep', self.on_beep)
                except TypeError:
                    err('beep signal unavailable with this version of VTE')

        self.vte.set_scrollback_lines(self.config['scrollback_lines'])
        self.vte.set_scroll_on_keystroke(self.config['scroll_on_keystroke'])
        self.vte.set_scroll_on_output(self.config['scroll_on_output'])

        if self.scrollbar_position != self.config['scrollbar_position']:
            self.scrollbar_position = self.config['scrollbar_position']
            if self.config['scrollbar_position'] == 'disabled':
                self.scrollbar.hide()
            else:
                self.scrollbar.show()
                if self.config['scrollbar_position'] == 'left':
                    self.reorder_child(self.scrollbar, 0)
                elif self.config['scrollbar_position'] == 'right':
                    self.reorder_child(self.vte, 0)

        if hasattr(self.vte, 'set_alternate_screen_scroll'):
            self.vte.set_alternate_screen_scroll(self.config['alternate_screen_scroll'])

        self.titlebar.update()
        self.vte.queue_draw()

    def get_window_title(self):
        """Return the window title"""
        return(self.vte.get_window_title() or str(self.command))

    def on_group_button_press(self, widget, event):
        """Handler for the group button"""
        if event.button == 1:
            self.create_popup_group_menu(widget, event)
        return(False)

    def on_keypress(self, widget, event):
        """Handler for keyboard events"""
        if not event:
            dbg('Terminal::on_keypress: Called on %s with no event' % widget)
            return(False)

        # FIXME: Does keybindings really want to live in Terminator()?
        mapping = self.terminator.keybindings.lookup(event)

        if mapping == "hide_window":
            return(False)

        if mapping and mapping not in ['close_window', 
                                       'full_screen', 
                                       'new_tab']:
            dbg('Terminal::on_keypress: lookup found: %r' % mapping)
            # handle the case where user has re-bound copy to ctrl+<key>
            # we only copy if there is a selection otherwise let it fall through
            # to ^<key>
            if (mapping == "copy" and event.state & gtk.gdk.CONTROL_MASK):
                if self.vte.get_has_selection ():
                    getattr(self, "key_" + mapping)()
                    return(True)
            else:
                getattr(self, "key_" + mapping)()
                return(True)

        # FIXME: This is all clearly wrong. We should be doing this better
        #         maybe we can emit the key event and let Terminator() care?
        groupsend = self.terminator.groupsend
        groupsend_type = self.terminator.groupsend_type
        if groupsend != groupsend_type['off'] and self.vte.is_focus():
            if self.group and groupsend == groupsend_type['group']:
                self.terminator.group_emit(self, self.group, 'key-press-event',
                        event)
            if groupsend == groupsend_type['all']:
                self.terminator.all_emit(self, 'key-press-event', event)

        return(False)

    def on_buttonpress(self, widget, event):
        """Handler for mouse events"""
        # Any button event should grab focus
        widget.grab_focus()

        if event.button == 1:
            # Ctrl+leftclick on a URL should open it
            if event.state & gtk.gdk.CONTROL_MASK == gtk.gdk.CONTROL_MASK:
                url = self.check_for_url(event)
                if url:
                    self.open_url(url, prepare=True)
        elif event.button == 2:
            # middleclick should paste the clipboard
            self.paste_clipboard(True)
            return(True)
        elif event.button == 3:
            # rightclick should display a context menu if Ctrl is not pressed
            if event.state & gtk.gdk.CONTROL_MASK == 0:
                self.popup_menu(widget, event)
                return(True)

        return(False)
    
    def popup_menu(self, widget, event=None):
        """Display the context menu"""
        menu = TerminalPopupMenu(self)
        menu.show(widget, event)

    def do_scrollbar_toggle(self):
        self.toggle_widget_visibility(self.scrollbar)

    def toggle_widget_visibility(self, widget):
        if widget.get_property('visible'):
            widget.hide()
        else:
            widget.show()

    def on_encoding_change(self, widget, encoding):
        """Handle the encoding changing"""
        current = self.vte.get_encoding()
        if current != encoding:
            dbg('on_encoding_change: setting encoding to: %s' % encoding)
            self.custom_encoding = not (encoding == self.config['encoding'])
            self.vte.set_encoding(encoding)

    def on_drag_begin(self, widget, drag_context, data):
        """Handle the start of a drag event"""
        widget.drag_source_set_icon_pixbuf(util.widget_pixbuf(self, 512))

    def on_drag_data_get(self, widget, drag_context, selection_data, info, time,
            data):
        """I have no idea what this does, drag and drop is a mystery. sorry."""
        selection_data.set('vte', info,
                str(data.terminator.terminals.index(self)))

    def on_drag_motion(self, widget, drag_context, x, y, time, data):
        """*shrug*"""
        if 'text/plain' in drag_context.targets:
            # copy text from another widget
            return
        srcwidget = drag_context.get_source_widget()
        if(isinstance(srcwidget, gtk.EventBox) and 
           srcwidget == self.titlebar) or widget == srcwidget:
            # on self
            return

        alloc = widget.allocation
        rect = gtk.gdk.Rectangle(0, 0, alloc.width, alloc.height)

        if self.config['use_theme_colors']:
            color = self.vte.get_style().text[gtk.STATE_NORMAL]
        else:
            color = gtk.gdk.color_parse(self.config['foreground_color'])

        pos = self.get_location(widget, x, y)
        topleft = (0,0)
        topright = (alloc.width,0)
        topmiddle = (alloc.width/2,0)
        bottomleft = (0, alloc.height)
        bottomright = (alloc.width,alloc.height)
        bottommiddle = (alloc.width/2, alloc.height)
        middle = (alloc.width/2, alloc.height/2)
        middleleft = (0, alloc.height/2)
        middleright = (alloc.width, alloc.height/2)
        #print "%f %f %d %d" %(coef1, coef2, b1,b2)
        coord = ()
        if pos == "right":
            coord = (topright, topmiddle, bottommiddle, bottomright)
        elif pos == "top":
            coord = (topleft, topright, middleright , middleleft)
        elif pos == "left":
            coord = (topleft, topmiddle, bottommiddle, bottomleft)
        elif pos == "bottom":
            coord = (bottomleft, bottomright, middleright , middleleft) 

        #here, we define some widget internal values
        widget._expose_data = { 'color': color, 'coord' : coord }
        #redraw by forcing an event
        connec = widget.connect_after('expose-event', self.on_expose_event)
        widget.window.invalidate_rect(rect, True)
        widget.window.process_updates(True)
        #finaly reset the values
        widget.disconnect(connec)
        widget._expose_data = None

    def on_expose_event(self, widget, event):
        """Handle an expose event while dragging"""
        if not widget._expose_data:
            return(False)

        color = widget._expose_data['color']
        coord = widget._expose_data['coord']

        context = widget.window.cairo_create()
        context.set_source_rgba(color.red, color.green, color.blue, 0.5)
        if len(coord) > 0 :
            context.move_to(coord[len(coord)-1][0],coord[len(coord)-1][1])
            for i in coord:
                context.line_to(i[0],i[1])

        context.fill()
        return(False)

    def on_drag_data_received(self, widget, drag_context, x, y, selection_data,
            info, time, data):
        if selection_data.type == 'text/plain':
            # copy text to destination
            txt = selection_data.data.strip()
            if txt[0:7] == 'file://':
                text = "'%s'" % urllib.unquote(txt[7:])
            for term in self.terminator.get_target_terms(self):
                term.feed(txt)
            return
        
        widgetsrc = data.terminator.terminals[int(selection_data.data)]
        srcvte = drag_context.get_source_widget()
        #check if computation requireds
        if (isinstance(srcvte, gtk.EventBox) and 
                srcvte == self.titlebar) or srcvte == widget:
            return

        srchbox = widgetsrc
        dsthbox = widget.get_parent().get_parent()

        dstpaned = dsthbox.get_parent()
        srcpaned = srchbox.get_parent()
        if isinstance(dstpaned, gtk.Window) and \
           isinstance(srcpaned, gtk.Window):
            return

        pos = self.get_location(widget, x, y)

        srcpaned.remove(widgetsrc)
        dstpaned.split_axis(dsthbox, pos in ['top', 'bottom'], widgetsrc)
        srcpaned.hoover()

    def get_location(self, vte, x, y):
        """Get our location within the terminal"""
        pos = ''
        #get the diagonales function for the receiving widget
        coef1 = float(vte.allocation.height)/float(vte.allocation.width)
        coef2 = -float(vte.allocation.height)/float(vte.allocation.width)
        b1 = 0
        b2 = vte.allocation.height
        #determine position in rectangle
        """
        --------
        |\    /|
        | \  / |
        |  \/  |
        |  /\  |
        | /  \ |
        |/    \|
        --------
        """
        if (x*coef1 + b1 > y ) and (x*coef2 + b2 < y ):
            pos =  "right"
        if (x*coef1 + b1 > y ) and (x*coef2 + b2 > y ):
            pos = "top"
        if (x*coef1 + b1 < y ) and (x*coef2 + b2 > y ):
            pos = "left"
        if (x*coef1 + b1 < y ) and (x*coef2 + b2 < y ):
            pos = "bottom"
        return pos

    def grab_focus(self):
        self.vte.grab_focus()

    def on_vte_focus(self, widget):
        self.emit('title-change', self.get_window_title())

    def on_vte_focus_out(self, widget, event):
        return

    def on_vte_focus_in(self, widget, event):
        self.emit('focus-in')

    def scrollbar_jump(self, position):
        """Move the scrollbar to a particular row"""
        self.scrollbar.set_value(position)

    def scrollbar_position(self):
        """Return the current position of the scrollbar"""
        return(self.scrollbar.get_value())

    def on_search_done(self, widget):
        """We've finished searching, so clean up"""
        self.searchbar.hide()
        self.scrollbar.set_value(self.vte.get_cursor_position()[1])
        self.vte.grab_focus()

    def on_edit_done(self, widget):
        """A child widget is done editing a label, return focus to VTE"""
        self.vte.grab_focus()

    def on_vte_size_allocate(self, widget, allocation):
        self.titlebar.update_terminal_size(self.vte.get_column_count(),
                self.vte.get_row_count())
        if self.vte.window and self.config['geometry_hinting']:
            window = util.get_top_window(self)
            window.set_rough_geometry_hints()

    def on_vte_notify_enter(self, term, event):
        """Handle the mouse entering this terminal"""
        if self.config['focus'] in ['sloppy', 'mouse']:
            if self.titlebar.editing() == False:
                term.grab_focus()
                return(False)

    def get_zoom_data(self):
        """Return a dict of information for Window"""
        data = {}
        data['old_font'] = self.vte.get_font()
        data['old_char_height'] = self.vte.get_char_height()
        data['old_char_width'] = self.vte.get_char_width()
        data['old_allocation'] = self.vte.get_allocation()
        data['old_padding'] = self.vte.get_padding()
        data['old_columns'] = self.vte.get_column_count()
        data['old_rows'] = self.vte.get_row_count()
        data['old_parent'] = self.get_parent()

        return(data)

    def zoom_scale(self, widget, allocation, old_data):
        """Scale our font correctly based on how big we are not vs before"""
        self.cnxids.remove_signal(self, 'zoom')

        new_columns = self.vte.get_column_count()
        new_rows = self.vte.get_row_count()
        new_font = self.vte.get_font()
        new_allocation = self.vte.get_allocation()

        old_alloc = {'x': old_data['old_allocation'].width - \
                          old_data['old_padding'][0],
                     'y': old_data['old_allocation'].height - \
                          old_data['old_padding'][1]
                    }

        dbg('Terminal::zoom_scale: Resized from %dx%d to %dx%d' % (
             old_data['old_columns'],
             old_data['old_rows'],
             new_columns,
             new_rows))

        if new_rows == old_data['old_rows'] or \
           new_columns == old_data['old_columns']:
            dbg('Terminal::zoom_scale: One axis unchanged, not scaling')
            return

        old_area = old_data['old_columns'] * old_data['old_rows']
        new_area = new_columns * new_rows
        area_factor = (new_area / old_area) / 2

        new_font.set_size(old_data['old_font'].get_size() * area_factor)
        self.vte.set_font(new_font)

    def is_zoomed(self):
        """Determine if we are a zoomed terminal"""
        prop = None
        parent = self.get_parent()
        window = get_top_window(self)

        try:
            prop = window.get_property('term-zoomed')
        except TypeError:
            prop = False

        return(prop)

    def zoom(self, widget=None):
        """Zoom ourself to fill the window"""
        self.emit('zoom')

    def maximise(self, widget=None):
        """Maximise ourself to fill the window"""
        self.emit('maximise')

    def unzoom(self, widget=None):
        """Restore normal layout"""
        self.emit('unzoom')

    def spawn_child(self, widget=None):
        update_records = self.config['update_records']
        login = self.config['login_shell']
        args = []
        shell = None
        command = None

        self.vte.grab_focus()

        options = self.config.options_get()
        if options.command:
            command = options.command
            options.command = None
        elif options.execute:
            command = options.execute
            options.execute = None
        elif self.config['use_custom_command']:
            command = self.config['custom_command']

        if type(command) is list:
            shell = util.path_lookup(command[0])
            args = command
        else:
            shell = util.shell_lookup()

            if self.config['login_shell']:
                args.insert(0, "-%s" % shell)
            else:
                args.insert(0, shell)

            if command is not None:
                args += ['-c', command]

        if shell is None:
            self.vte.feed(_('Unable to find a shell'))
            return(-1)

        try:
            os.putenv('WINDOWID', '%s' % self.vte.get_parent_window().xid)
        except AttributeError:
            pass

        dbg('Forking shell: "%s" with args: %s' % (shell, args))
        self.pid = self.vte.fork_command(command=shell, argv=args, envv=[],
                loglastlog=login, logwtmp=update_records,
                logutmp=update_records, directory=self.cwd)
        self.command = shell

        self.titlebar.update()

        if self.pid == -1:
            self.vte.feed(_('Unable to start shell:') + shell)
            return(-1)

    def check_for_url(self, event):
        """Check if the mouse is over a URL"""
        return (self.vte.match_check(int(event.x / self.vte.get_char_width()),
            int(event.y / self.vte.get_char_height())))

    def prepare_url(self, urlmatch):
        """Prepare a URL from a VTE match"""
        url = urlmatch[0]
        match = urlmatch[1]

        if match == self.matches['email'] and url[0:7] != 'mailto:':
            url = 'mailto:' + url
        elif match == self.matches['addr_only'] and url[0:3] == 'ftp':
            url = 'ftp://' + url
        elif match == self.matches['addr_only']:
            url = 'http://' + url
        elif match in self.matches.values():
            # We have a match, but it's not a hard coded one, so it's a plugin
            try:
                registry = plugin.PluginRegistry()
                registry.load_plugins()
                plugins = registry.get_plugins_by_capability('url_handler')

                for urlplugin in plugins:
                    if match == self.matches[urlplugin.handler_name]:
                        newurl = urlplugin.callback(url)
                        if newurl is not None:
                            dbg('Terminal::prepare_url: URL prepared by \
%s plugin' % urlplugin.handler_name)
                            url = newurl
                        break;
            except Exception, ex:
                err('Terminal::prepare_url: %s' % ex)

        return(url)

    def open_url(self, url, prepare=False):
        """Open a given URL, conditionally unpacking it from a VTE match"""
        if prepare == True:
            url = self.prepare_url(url)
        dbg('open_url: URL: %s (prepared: %s)' % (url, prepare))
        gtk.show_uri(None, url, gtk.gdk.CURRENT_TIME)

    def paste_clipboard(self, primary=False):
        """Paste one of the two clipboards"""
        for term in self.terminator.get_target_terms(self):
            if primary:
                term.vte.paste_primary()
            else:
                term.vte.paste_clipboard()
        self.vte.grab_focus()

    def feed(self, text):
        """Feed the supplied text to VTE"""
        self.vte.feed_child(text)

    def zoom_in(self):
        """Increase the font size"""
        self.zoom_font(True)

    def zoom_out(self):
        """Decrease the font size"""
        self.zoom_font(False)

    def zoom_font(self, zoom_in):
        """Change the font size"""
        pangodesc = self.vte.get_font()
        fontsize = pangodesc.get_size()

        if fontsize > pango.SCALE and not zoom_in:
            fontsize -= pango.SCALE
        elif zoom_in:
            fontsize += pango.SCALE

        pangodesc.set_size(fontsize)
        self.vte.set_font(pangodesc)
        self.custom_font_size = fontsize

    def zoom_orig(self):
        """Restore original font size"""
        dbg("Terminal::zoom_orig: restoring font to: %s" % self.config['font'])
        self.vte.set_font(pango.FontDescription(self.config['font']))
        self.custom_font_size = None

    def get_cursor_position(self):
        """Return the co-ordinates of our cursor"""
        col, row = self.vte.get_cursor_position()
        width = self.vte.get_char_width()
        height = self.vte.get_char_height()
        return((col * width, row * height))

    def get_font_size(self):
        """Return the width/height of our font"""
        return((self.vte.get_char_width(), self.vte.get_char_height()))

    def get_size(self):
        """Return the column/rows of the terminal"""
        return((self.vte.get_column_count(), self.vte.get_row_count()))

    def on_beep(self, widget):
        """Set the urgency hint for our window"""
        window = util.get_top_window(self)
        window.set_urgency_hint(True)

    # There now begins a great list of keyboard event handlers
    # FIXME: Probably a bunch of these are wrong. TEST!
    def key_zoom_in(self):
        self.zoom_in()

    def key_zoom_out(self):
        self.zoom_out()

    def key_copy(self):
        self.vte.copy_clipboard()

    def key_paste(self):
        self.vte.paste_clipboard()

    def key_toggle_scrollbar(self):
        self.do_scrollbar_toggle()

    def key_zoom_normal(self):
        self.zoom_orig ()

    def key_search(self):
        self.searchbar.start_search()

    # bindings that should be moved to Terminator as they all just call
    # a function of Terminator. It would be cleaner if TerminatorTerm
    # has absolutely no reference to Terminator.
    # N (next) - P (previous) - O (horizontal) - E (vertical) - W (close)
    def key_new_root_tab(self):
        self.terminator.newtab (self, True)

    def key_cycle_next(self):
        self.key_go_next()

    def key_cycle_prev(self):
        self.key_go_prev()

    def key_go_next(self):
        self.emit('navigate', 'next')

    def key_go_prev(self):
        self.emit('navigate', 'prev')

    def key_go_up(self):
        self.emit('navigate', 'up')

    def key_go_down(self):
        self.emit('navigate', 'down')

    def key_go_left(self):
        self.emit('navigate', 'left')

    def key_go_right(self):
        self.emit('navigate', 'right')

    def key_split_horiz(self):
        self.emit('split-horiz')

    def key_split_vert(self):
        self.emit('split-vert')

    def key_close_term(self):
        self.close()

    def key_resize_up(self):
        self.emit('resize-term', 'up')

    def key_resize_down(self):
        self.emit('resize-term', 'down')

    def key_resize_left(self):
        self.emit('resize-term', 'left')

    def key_resize_right(self):
        self.emit('resize-term', 'right')

    def key_move_tab_right(self):
        self.terminator.move_tab (self, 'right')

    def key_move_tab_left(self):
        self.terminator.move_tab (self, 'left')

    def key_toggle_zoom(self):
        if self.is_zoomed():
            self.unzoom()
        else:
            self.maximise()

    def key_scaled_zoom(self):
        if self.is_zoomed():
            self.unzoom()
        else:
            self.zoom()

    def key_next_tab(self):
        self.emit('tab-change', -1)

    def key_prev_tab(self):
        self.emit('tab-change', -2)

    def key_switch_to_tab_1(self):
        self.emit('tab-change', 0)

    def key_switch_to_tab_2(self):
        self.emit('tab-change', 1)

    def key_switch_to_tab_3(self):
        self.emit('tab-change', 2)

    def key_switch_to_tab_4(self):
        self.emit('tab-change', 3)

    def key_switch_to_tab_5(self):
        self.emit('tab-change', 4)

    def key_switch_to_tab_6(self):
        self.emit('tab-change', 5)

    def key_switch_to_tab_7(self):
        self.emit('tab-change', 6)

    def key_switch_to_tab_8(self):
        self.emit('tab-change', 7)

    def key_switch_to_tab_9(self):
        self.emit('tab-change', 8)

    def key_switch_to_tab_10(self):
        self.emit('tab-change', 9)

    def key_reset(self):
        self.vte.reset (True, False)

    def key_reset_clear(self):
        self.vte.reset (True, True)

    def key_group_all(self):
        self.emit('group-all')

    def key_ungroup_all(self):
        self.emit('ungroup-all')

    def key_group_tab(self):
        self.emit('group-tab')
    
    def key_ungroup_tab(self):
        self.emit('ungroup-tab')

    def key_new_window(self):
        cmd = sys.argv[0]
    
        if not os.path.isabs(cmd):
            # Command is not an absolute path. Figure out where we are
            cmd = os.path.join (self.cwd, sys.argv[0])
            if not os.path.isfile(cmd):
                # we weren't started as ./terminator in a path. Give up
                err('Terminal::key_new_window: Unable to locate Terminator')
                return False
          
        dbg("Terminal::key_new_window: Spawning: %s" % cmd)
        subprocess.Popen([cmd,])
# End key events

gobject.type_register(Terminal)
# vim: set expandtab ts=4 sw=4:
