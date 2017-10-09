from __future__ import print_function

import gevent
import string
import functools
import virtualbox
import subprocess

from gevent.lock import Semaphore

from disco.bot import Plugin, Config, CommandLevels

ZERO_WIDTH_SPACE = u'\u200B'
THUMBS_UP = u'\U0001f44d'
THUMBS_DOWN = u'\U0001f44e'


def locked(f):
    @functools.wraps(f)
    def wrapped(self, *args, **kwargs):
        with self.lock:
            res = f(self, *args, **kwargs)
        return res
    return wrapped


def C(txt):
    return txt.replace('@', '@' + ZERO_WIDTH_SPACE)


class VNCBotPluginConfig(Config):
    vm_name = None
    snapshot = None

    cooldown = 30
    channels = []


@Plugin.with_config(VNCBotPluginConfig)
class VNCBotPlugin(Plugin):
    def load(self, ctx):
        super(VNCBotPlugin, self).load(ctx)

        self.live = None
        self.lock = Semaphore()
        self.vbox = virtualbox.VirtualBox()
        self.vm = self.vbox.find_machine(self.config.vm_name)
        self.session = self.vm.create_session()

    def post_screenshot(self, event, update=False):
        h, w, _, _, _, _ = self.session.console.display.get_screen_resolution(0)
        png = self.session.console.display.take_screen_shot_to_array(0, h, w, virtualbox.library.BitmapFormat.png)
        if update:
            event.delete()
            return event.reply('', attachment=('screenshot.png', png))
        return event.msg.reply('', attachment=('screenshot.png', png))

    def clear_cooldown(self, overwrite):
        gevent.sleep(self.config.cooldown)
        overwrite.delete()

    def cooldown_user(self, event):
        level = self.bot.get_level(event.guild.get_member(event.author.id) if event.guild else event.author)

        if level < CommandLevels.ADMIN and event.channel.id not in self.config.channels:
            return

        overwrite = event.channel.create_overwrite(event.author, deny=2048)
        gevent.spawn(self.clear_cooldown, overwrite)

    def restore(self):
        self.session.console.power_down().wait_for_completion(10 * 1000)
        self.session.unlock_machine()

        subprocess.check_call("VBoxManage snapshot '{}' restore {}".format(
            self.config.vm_name, self.config.snapshot
        ), shell=True)

        self.vm.launch_vm_process(self.session, 'headless', '')
        gevent.sleep(20)

    @Plugin.command('reset', level=CommandLevels.ADMIN)
    def reset(self, event):
        msg = event.msg.reply('Reseting...')
        self.restore()
        msg.edit('Reseting... DONE!')

    @Plugin.command('vote reset', level=1)
    def vote_reset(self, event):
        msg = event.msg.reply('**VOTE TO RESET** _you have 20 seconds to cast your vote_')
        msg.create_reaction(THUMBS_UP)
        msg.create_reaction(THUMBS_DOWN)
        self.cooldown_user(event)
        gevent.sleep(20)

        # Reget the message
        msg = msg.channel.get_message(msg)

        yes = next((i.count for i in msg.reactions if i.emoji.name == THUMBS_UP), 0)
        no = next((i.count for i in msg.reactions if i.emoji.name == THUMBS_DOWN), 0)
        msg.edit('{} YES | {} NO -- VOTE {}'.format(yes, no, 'PASSED' if yes > no else 'FAILED'))

        if yes > no:
            self.restore()
            self.post_screenshot(event)

    @Plugin.command('screenshot', level=CommandLevels.ADMIN)
    def on_screenshot(self, event):
        self.post_screenshot(event)

    @Plugin.command('live', '<task:str>', level=CommandLevels.ADMIN)
    def on_live(self, event, task):
        if task == 'on':
            if self.live:
                return event.msg.reply('Already live')

            def live():
                msg = self.post_screenshot(event)

                while True:
                    gevent.sleep(5)
                    msg = self.post_screenshot(msg, update=True)
            self.live = gevent.spawn(live)
        else:
            if not self.live:
                return event.msg.reply('Not live')
            self.live.kill()

    @Plugin.command('keys')
    def on_keys(self, event):
        event.msg.reply('```{}```'.format(
            ', '.join(sorted(map(lambda z: repr(z)[1:-1], self.session.console.keyboard.SCANCODES.keys())))
        ))

    @Plugin.command('mouse', '<x:int> <y:int> [sx:int] [sy:int]')
    @locked
    def on_mouse(self, event, x, y, sx=0, sy=0):
        if event.channel.id not in self.config.channels:
            return
        if x > 100 or y > 100 or x < -100 or y < -100:
            return event.msg.reply('Movement cannot be above/below 100!')
        self.cooldown_user(event)
        self.session.console.mouse.put_mouse_event(x, y, sx, sy, 0)
        self.post_screenshot(event)

    @Plugin.command('click', '<button:str>')
    @locked
    def on_click(self, event, button):
        if event.channel.id not in self.config.channels:
            return
        flags = 0

        if button == 'left':
            flags = 0x01
        elif button == 'right':
            flags = 0x02
        elif button == 'middle':
            flags = 0x03
        else:
            return event.msg.reply('Invalid button, must be left/right/middle!')

        self.cooldown_user(event)
        self.session.console.mouse.put_mouse_event(0, 0, 0, 0, flags)
        self.session.console.mouse.put_mouse_event(0, 0, 0, 0, 0)
        self.post_screenshot(event)

    @Plugin.command('press', '<button:str>')
    @locked
    def press(self, event, button):
        if event.channel.id not in self.config.channels:
            return
        flags = 0

        if button == 'left':
            flags = 0x01
        elif button == 'right':
            flags = 0x02
        elif button == 'middle':
            flags = 0x03
        else:
            return event.msg.reply('Invalid button, must be left/right/middle!')

        self.cooldown_user(event)
        self.session.console.mouse.put_mouse_event(0, 0, 0, 0, flags)
        self.post_screenshot(event)

    @Plugin.command('release')
    @locked
    def release(self, event):
        if event.channel.id not in self.config.channels:
            return
        self.cooldown_user(event)
        self.session.console.mouse.put_mouse_event(0, 0, 0, 0, 0)
        self.post_screenshot(event)

    @Plugin.command('key', '<key:str>')
    @locked
    def on_key(self, event, key):
        if event.channel.id not in self.config.channels:
            return

        press, hold = [], []

        if '+' in key:
            mods, key = key.rsplit('+', 1)

            for mod in mods.split('+'):
                if len(mod) == 1 or mod.upper() not in self.session.console.keyboard.SCANCODES:
                    return event.msg.reply('Invalid modifier: `{}`'.format(C(mod)))

                hold.append(mod.upper())

        if len(key) == 1 and key in string.printable:
            press.append(key)
            if key.isupper():
                hold.append('LSHIFT')
        elif key.upper() in self.session.console.keyboard.SCANCODES:
            press.append(key.upper())
        else:
            return event.msg.reply('Invalid key: `{}`'.format(C(key)))

        self.cooldown_user(event)
        self.session.console.keyboard.put_keys(press, hold)
        self.post_screenshot(event)
