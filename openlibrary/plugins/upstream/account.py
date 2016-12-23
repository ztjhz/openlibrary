import web
import hmac
import logging
import random
import urllib
import uuid
import datetime, time
import simplejson

from infogami.utils import delegate
from infogami import config
from infogami.utils.view import (
    require_login, render, render_template, add_flash_message)
from infogami.infobase.client import ClientException
from infogami.utils.context import context
import infogami.core.code as core

from openlibrary.i18n import gettext as _
from openlibrary.core import helpers as h
from openlibrary.plugins.recaptcha import recaptcha
from openlibrary import accounts
from openlibrary.accounts import (
    Account, InternetArchiveAccount, OpenLibraryAccount, valid_email)
import forms
import utils
import borrow


logger = logging.getLogger("openlibrary.account")

# XXX: These need to be cleaned up
send_verification_email = accounts.send_verification_email
create_link_doc = accounts.create_link_doc
sendmail = accounts.sendmail

class account(delegate.page):
    """Account preferences.
    """
    @require_login
    def GET(self):
        user = accounts.get_current_user()
        return render.account(user)

class account_create(delegate.page):
    """New account creation.

    Account will in the pending state until the email is activated.
    """
    path = "/account/create"

    def GET(self):
        f = self.get_form()
        return render['account/create'](f)

    def get_form(self):
        f = forms.Register()
        recap = self.get_recap()
        f.has_recaptcha = recap is not None
        if f.has_recaptcha:
            f.inputs = list(f.inputs) + [recap]
        return f

    def get_recap(self):
        if self.is_plugin_enabled('recaptcha'):
            public_key = config.plugin_recaptcha.public_key
            private_key = config.plugin_recaptcha.private_key
            return recaptcha.Recaptcha(public_key, private_key)

    def is_plugin_enabled(self, name):
        return name in delegate.get_plugins() or "openlibrary.plugins." + name in delegate.get_plugins()

    def POST(self):
        i = web.input('email', 'password', 'username', agreement="no")
        i.displayname = i.get('displayname') or i.username

        f = self.get_form()
        if not f.validates(i):
            return render['account/create'](f)

        if i.agreement != "yes":
            f.note = utils.get_error("account_create_tos_not_selected")
            return render['account/create'](f)

        try:
            accounts.register(username=i.username,
                              email=i.email,
                              password=i.password,
                              displayname=i.displayname)
        except ClientException, e:
            f.note = str(e)
            return render['account/create'](f)

        send_verification_email(i.username, i.email)
        return render['account/verify'](username=i.username, email=i.email)

del delegate.pages['/account/register']

class account_login(delegate.page):
    """Account login.

    Login can fail because of the following reasons:

    * account_not_found: Error message is displayed.
    * account_bad_password: Error message is displayed with a link to reset password.
    * account_not_verified: Error page is dispalyed with button to "resend verification email".
    """
    path = "/account/login"

    def GET(self):
        referer = web.ctx.env.get('HTTP_REFERER', '/')
        i = web.input(redirect=referer)
        f = forms.Login()
        f['redirect'].value = i.redirect
        return render.login(f)

    def POST(self):
        i = web.input(email='', connect=None, remember=False,
                      redirect='/', action="login")

        if i.action == "resend_verification_email":
            return self.POST_resend_verification_email(i)
        else:
            return self.POST_login(i)

    def error(self, name, i):
        f = forms.Login()
        f.fill(i)
        f.note = utils.get_error(name)
        return render.login(f)

    def error_check(self, audit, i):
        if 'error' in audit:
            error = audit['error']
            if error == "account_not_verified":
                # XXX this template will need to be updated
                return render_template(
                    "account/not_verified", username=account.username,
                    password=i.password, email=account.email)
            elif error == "account_not_found":
                return self.error("account_user_notfound", i)
            elif error == "account_blocked":
                return self.error("account_blocked", i)
            else:
                return self.error(audit['error'], i)
        if not audit['link']:
            # This needs to be overriden w/ `test`
            return self.error("accounts_not_connected", i)
        return None

    def POST_login(self, i):
        i = web.input(username="", password="")

        audit = audit_accounts(i.username, i.password)
        errors = self.error_check(audit, i)
        if errors:
            return errors

        if i.redirect == "/account/login" or i.redirect == "":
            i.redirect = "/"

        expires = (i.remember and 3600 * 24 * 7) or ""
        web.setcookie(config.login_cookie_name, web.ctx.conn.get_auth_token(),
                      expires=expires)
        raise web.seeother(i.redirect)

    def POST_resend_verification_email(self, i):
        try:
            accounts.login(i.username, i.password)
        except ClientException, e:
            code = e.get_data().get("code")
            if code != "account_not_verified":
                return self.error("account_incorrect_password", i)

        account = accounts.find(username=i.username)
        account.send_verification_email()

        title = _("Hi %(user)s", user=account.displayname)
        message = _("We've sent the verification email to %(email)s. You'll need to read that and click on the verification link to verify your email.", email=account.email)
        return render.message(title, message)

class account_verify(delegate.page):
    """Verify user account.
    """
    path = "/account/verify/([0-9a-f]*)"

    def GET(self, code):
        docs = web.ctx.site.store.values(type="account-link", name="code", value=code)
        if docs:
            doc = docs[0]

            account = accounts.find(username = doc['username'])
            if account:
                if account['status'] != "pending":
                    return render['account/verify/activated'](account)
            account.activate()
            user = web.ctx.site.get("/people/" + doc['username']) #TBD
            return render['account/verify/success'](account)
        else:
            return render['account/verify/failed']()

    def POST(self, code=None):
        """Called to regenerate account verification code.
        """
        i = web.input(email=None)
        account = accounts.find(email=i.email)
        if not account:
            return render_template("account/verify/failed", email=i.email)
        elif account['status'] != "pending":
            return render['account/verify/activated'](account)
        else:
            account.send_verification_email()
            title = _("Hi %(user)s", user=account.displayname)
            message = _("We've sent the verification email to %(email)s. You'll need to read that and click on the verification link to verify your email.", email=account.email)
            return render.message(title, message)

class account_verify_old(account_verify):
    """Old account verification code.

    This takes username, email and code as url parameters. The new one takes just the code as part of the url.
    """
    path = "/account/verify"
    def GET(self):
        # It is too long since we switched to the new account verification links.
        # All old links must be expired by now.
        # Show failed message without thinking.
        return render['account/verify/failed']()

class account_email(delegate.page):
    """Change email.
    """
    path = "/account/email"

    def get_email(self):
        user = accounts.get_current_user()
        return user.get_account()['email']

    @require_login
    def GET(self):
        f = forms.ChangeEmail()
        return render['account/email'](self.get_email(), f)

    @require_login
    def POST(self):
        f = forms.ChangeEmail()
        i = web.input()

        if not f.validates(i):
            return render['account/email'](self.get_email(), f)
        else:
            user = accounts.get_current_user()
            username = user.key.split('/')[-1]

            displayname = user.displayname or username

            send_email_change_email(username, i.email)

            title = _("Hi %(user)s", user=user.displayname or username)
            message = _("We've sent an email to %(email)s. You'll need to read that and click on the verification link to update your email.", email=i.email)
            return render.message(title, message)

class account_email_verify(delegate.page):
    path = "/account/email/verify/([0-9a-f]*)"

    def GET(self, code):
        link = accounts.get_link(code)
        if link:
            username = link['username']
            email = link['email']
            link.delete()
            return self.update_email(username, email)
        else:
            return self.bad_link()

    def update_email(self, username, email):
        if accounts.find(email=email):
            title = _("Email address is already used.")
            message = _("Your email address couldn't be updated. The specified email address is already used.")
        else:
            logger.info("updated email of %s to %s", username, email)
            accounts.update_account(username=username, email=email, status="active")
            title = _("Email verification successful.")
            message = _('Your email address has been successfully verified and updated in your account.')
        return render.message(title, message)

    def bad_link(self):
        title = _("Email address couldn't be verified.")
        message = _("Your email address couldn't be verified. The verification link seems invalid.")
        return render.message(title, message)

class account_email_verify_old(account_email_verify):
    path = "/account/email/verify"

    def GET(self):
        # It is too long since we switched to the new email verification links.
        # All old links must be expired by now.
        # Show failed message without thinking.
        return self.bad_link()

class account_password(delegate.page):
    path = "/account/password"

    @require_login
    def GET(self):
        f = forms.ChangePassword()
        return render['account/password'](f)

    @require_login
    def POST(self):
        f = forms.ChangePassword()
        i = web.input()

        if not f.validates(i):
            return render['account/password'](f)

        user = accounts.get_current_user()
        username = user.key.split("/")[-1]

        if self.try_login(username, i.password):
            accounts.update_account(username, password=i.new_password)
            add_flash_message('note', _('Your password has been updated successfully.'))
            raise web.seeother('/account')
        else:
            f.note = "Invalid password"
            return render['account/password'](f)

    def try_login(self, username, password):
        account = accounts.find(username=username)
        return account and account.verify_password(password)

class account_password_forgot(delegate.page):
    path = "/account/password/forgot"

    def GET(self):
        f = forms.ForgotPassword()
        return render['account/password/forgot'](f)

    def POST(self):
        i = web.input(email='')

        f = forms.ForgotPassword()

        if not f.validates(i):
            return render['account/password/forgot'](f)

        account = accounts.find(email=i.email)

        if account.is_blocked():
            f.note = utils.get_error("account_blocked")
            return render_template('account/password/forgot', f)

        send_forgot_password_email(account.username, i.email)
        return render['account/password/sent'](i.email)

class account_password_reset(delegate.page):

    path = "/account/password/reset/([0-9a-f]*)"

    def GET(self, code):
        docs = web.ctx.site.store.values(type="account-link", name="code", value=code)
        if not docs:
            title = _("Password reset failed.")
            message = "Your password reset link seems invalid or expired."
            return render.message(title, message)

        f = forms.ResetPassword()
        return render['account/password/reset'](f)

    def POST(self, code):
        link = accounts.get_link(code)
        if not link:
            title = _("Password reset failed.")
            message = "The password reset link seems invalid or expired."
            return render.message(title, message)

        username = link['username']
        i = web.input()

        accounts.update_account(username, password=i.password)
        link.delete()
        return render_template("account/password/reset_success", username=username)


class check_username_available(delegate.page):

    path = "/account/check_username"

    def POST(self):
        """Checks whether `username` is availabe on service (i.e. `ia` or
        `ol`)"""
        i = web.input(service="ia", email="", password="")
        if i.service == 'ia':
            return accounts.username_available(username)
        elif i.service == 'ol':
            return

class account_connect(delegate.page):

    path = "/account/connect"

    def POST(self):
        """Links or creates accounts"""
        i = web.input(email="", password="", username="",
                      bridgeService="", bridgeEmail="", bridgePassword="",
                      test=False)
        result = link_accounts(i.get('email').lower(), i.password,
                               bridgeEmail=i.bridgeEmail.lower(),
                               bridgePassword=i.bridgePassword,
                               username=i.username, test=i.test)
        return delegate.RawText(simplejson.dumps(result),
                                content_type="application/json")


class account_audit(delegate.page):

    path = "/account/audit"

    def POST(self):
        i = web.input(email='', password='')
        test = i.get('test', '').lower() == 'true'
        email = i.get('email').lower()
        password = i.get('password')
        result = audit_accounts(email, password, test=test)
        return delegate.RawText(simplejson.dumps(result),
                                content_type="application/json")


def link_accounts(email, password, bridgeEmail="", bridgePassword="",
                  username="", test=False):

    audit = audit_accounts(email, password)

    if 'error' in audit:
        return audit

    ia_account = (InternetArchiveAccount.get(
        itemname=audit['has_ia'], test=i.test) if
                  audit.get('has_ia', False) else None)
    ol_account = (OpenLibraryAccount.get(email=email, test=test) if
                  audit.get('has_ol', False) else None)

    if ia_account and ol_account:
        if not audit['link']:
            audit['link'] = ia_account.itemname
            # XXX update and set once the db migration is complete
            # ol_account.archive_user_itemname = ia_account.itemname
            # ol_account._save()
            pass
        return audit
    elif not (ia_account or ol_account):
        return {'error': 'no_valid_accounts'}
    else:
        if bridgeEmail and bridgePassword:
            if not valid_email(bridgeEmail):
                return {'error': 'invalid_bridgeEmail'}
            if ol_account:
                ia_account = InternetArchiveAcccount.get(
                    email=bridgeEmail, test=test)
                if ia_account and ia_account.authenticates(bridgePassword):
                    #ol_account.archive_user_itemid = ia_account.itemname
                    #ol_account._save()
                    audit['has_ia'] = ia_account.itemname
                    return audit
                return {'error': 'invalid_ia_credentials'}
            elif ia_account:
                ol_account = OpenLibraryAccount.get(email=email, test=test)
                if ol_account.authenticated(password):
                    #ol_account.archive_user_itemid = ia_account.itemname
                    #ol_account._save()
                    audit['has_ol'] = ol_account.username
                    return audit
                return {'error': 'invalid_ol_credentials'}
        elif email and password and username:
            if ol_account:
                try:
                    ia_account = InternetArchiveAccount.create(
                        username, email, password, test=test)
                    audit['linked'] = username  # XXX should be itemname of new account
                    audit['has_ia'] = username  # XXX should be itemname of new account
                    return audit
                except (ValueError, NotImplementedError) as e:
                    return {'error': str(e)}
            elif ia_account:
                try:
                    ol_account = OpenLibraryAccount.create(
                        username, email, password, test=test)
                    # XXX set linked, etc
                    audit['linked'] = ia_account.itemname
                    audit['has_ol'] = username
                    return audit
                except (ValueError, NotImplementedError) as e:
                    return {'error': str(e)}
            return {'error': 'no_valid_accounts'}
        return {'error': 'email, password, and username required'}


def audit_accounts(email, password, test=False):
    if not valid_email(email):
        return {'error': 'invalid_email'}

    ol_account = OpenLibraryAccount.get(email=email, test=test)
    ia_account = (ol_account.get_linked_ia_account() if ol_account else
                  InternetArchiveAccount.get(email=email, test=test))

    audit = {
        'email': email,
        'authenticated': False,
        'has_ia': False,
        'has_ol': False,
        'link': ol_account.itemname if ol_account else None
    }

    if ol_account and ol_account.is_blocked():
        return {"error": "ol_account_blocked"}

    if not (ol_account or ia_account):
        return {'error': 'account_user_notfound'}

    if ia_account:
        audit['has_ia'] = ia_account.itemname
        if ia_account.authenticates(password):
            audit['authenticated'] = 'ia'

            if ol_account:
                audit['has_ol'] = ol_account.username
                audit['link'] = getattr(ol_account, 'archive_user_itemname', None)

            else:
                # check if there's an OL account which links to this
                # IA account (this IA account could have a different
                # email than the linked OL account)
                _link = ia_account.itemname
                ol_account = OpenLibraryAccount.get(link=_link, test=test)
                if ol_account:
                    audit['has_ol'] = ol_account.username
                    audit['link'] = _link

        # If IA is linked, only IA creds should be honored.
        if audit['link'] and not audit['authenticated']:
            return {'error': "wrong_ia_credentials"}

    if ol_account:
        audit['has_ol'] = ol_account.username
        if not audit['authenticated']:
            status = ol_account.login(password)
            if status == "ok":
                audit['authenticated'] = 'ol'
            else:
                return {'error': status}

        if not audit['authenticated']:
            return {'error': "wrong_ol_credentials"}

    # Links the accounts if they can be and are not already:
    if (audit['authenticated'] and not audit['link'] and
        audit['has_ia'] and audit['has_ol']):
        audit['link'] = ia_account.itemname
        audit['just_linked'] = True  # debug only
        # XXX once the db migration is complete:
        # ol_account.archive_user_itemname = ia_account.itemname
        # ol_account._save()

    return audit


class account_notifications(delegate.page):
    path = "/account/notifications"

    @require_login
    def GET(self):
        user = accounts.get_current_user()
        prefs = web.ctx.site.get(user.key + "/preferences")
        d = (prefs and prefs.get('notifications')) or {}
        email = accounts.get_current_user().email
        return render['account/notifications'](d, email)

    @require_login
    def POST(self):
        user = accounts.get_current_user()
        key = user.key + '/preferences'
        prefs = web.ctx.site.get(key)

        d = (prefs and prefs.dict()) or {'key': key, 'type': {'key': '/type/object'}}

        d['notifications'] = web.input()

        web.ctx.site.save(d, 'save notifications')

        add_flash_message('note', _("Notification preferences have been updated successfully."))
        web.seeother("/account")

class account_loans(delegate.page):
    path = "/account/loans"

    @require_login
    def GET(self):
        user = accounts.get_current_user()
        user.update_loan_status()
        loans = borrow.get_loans(user)
        return render['account/borrow'](user, loans)

class account_others(delegate.page):
    path = "(/account/.*)"

    def GET(self, path):
        return render.notfound(path, create=False)


####


def send_email_change_email(username, email):
    key = "account/%s/email" % username

    doc = create_link_doc(key, username, email)
    web.ctx.site.store[key] = doc

    link = web.ctx.home + "/account/email/verify/" + doc['code']
    msg = render_template("email/email/verify", username=username, email=email, link=link)
    sendmail(email, msg)

def send_forgot_password_email(username, email):
    key = "account/%s/password" % username

    doc = create_link_doc(key, username, email)
    web.ctx.site.store[key] = doc

    link = web.ctx.home + "/account/password/reset/" + doc['code']
    msg = render_template("email/password/reminder", username=username, link=link)
    sendmail(email, msg)




def as_admin(f):
    """Infobase allows some requests only from admin user. This decorator logs in as admin, executes the function and clears the admin credentials."""
    def g(*a, **kw):
        try:
            delegate.admin_login()
            return f(*a, **kw)
        finally:
            web.ctx.headers = []
    return g
