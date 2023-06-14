"""CILogon OAuthAuthenticator for JupyterHub

Uses OAuth 2.0 with cilogon.org (override with CILOGON_HOST)

Caveats:

- For allowed user list /admin purposes, username will be the ePPN by default.
  This is typically an email address and may not work as a Unix userid.
  Normalization may be required to turn the JupyterHub username into a Unix username.
- Default username_claim of ePPN does not work for all providers,
  e.g. generic OAuth such as Google.
  Use `c.CILogonOAuthenticator.username_claim = 'email'` to use
  email instead of ePPN as the JupyterHub username.
"""
import os
from urllib.parse import urlparse

import jsonschema
from jupyterhub.auth import LocalAuthenticator
from ruamel.yaml import YAML
from tornado import web
from traitlets import Bool, Dict, List, Unicode, default, validate

from .oauth2 import OAuthenticator, OAuthLoginHandler

yaml = YAML(typ="safe", pure=True)


class CILogonLoginHandler(OAuthLoginHandler):
    """See https://www.cilogon.org/oidc for general information."""

    def authorize_redirect(self, *args, **kwargs):
        """Add idp, skin to redirect params"""
        extra_params = kwargs.setdefault('extra_params', {})
        if self.authenticator.shown_idps:
            # selected_idp must be a string where idps are separated by commas, with no space between, otherwise it will get escaped
            # example: https://accounts.google.com/o/oauth2/auth,https://github.com/login/oauth/authorize
            idps = ",".join(self.authenticator.shown_idps)
            extra_params["selected_idp"] = idps
        if self.authenticator.skin:
            extra_params["skin"] = self.authenticator.skin

        return super().authorize_redirect(*args, **kwargs)


class CILogonOAuthenticator(OAuthenticator):
    _deprecated_oauth_aliases = {
        # <deprecated-config>:
        #   (
        #    <new-config>,
        #    <deprecation-version>,
        #    <deprecated-config-and-new-config-have-same-type>
        #   )
        "idp_whitelist": ("allowed_idps", "0.12.0", False),
        "idp": ("shown_idps", "15.0.0", False),
        "strip_idp_domain": ("allowed_idps", "15.0.0", False),
        **OAuthenticator._deprecated_oauth_aliases,
    }

    login_service = "CILogon"

    client_id_env = 'CILOGON_CLIENT_ID'
    client_secret_env = 'CILOGON_CLIENT_SECRET'

    user_auth_state_key = "cilogon_user"

    login_handler = CILogonLoginHandler

    cilogon_host = Unicode(os.environ.get("CILOGON_HOST") or "cilogon.org", config=True)

    @default("authorize_url")
    def _authorize_url_default(self):
        return f"https://{self.cilogon_host}/authorize"

    @default("token_url")
    def _token_url(self):
        return f"https://{self.cilogon_host}/oauth2/token"

    @default("userdata_url")
    def _userdata_url_default(self):
        return f"https://{self.cilogon_host}/oauth2/userinfo"

    @default("username_claim")
    def _username_claim_default(self):
        """What keys are available will depend on the scopes requested.
        See https://www.cilogon.org/oidc for details.
        Note that this option can be overridden for specific identity providers via `allowed_idps[<identity provider>]["username_derivation"]["username_claim"]`.
        """
        return "eppn"

    scope = List(
        Unicode(),
        default_value=['openid', 'email', 'org.cilogon.userinfo', 'profile'],
        config=True,
        help="""The OAuth scopes to request.

        See cilogon_scope.md for details.
        At least 'openid' is required.
        """,
    )

    @validate('scope')
    def _validate_scope(self, proposal):
        """Ensure `openid` is always requested and `org.cilogon.userinfo`
        is requested when allowed_idps is specified.
        """
        scopes = proposal.value

        if 'openid' not in proposal.value:
            scopes += ['openid']

        if self.allowed_idps and 'org.cilogon.userinfo' not in proposal.value:
            scopes += ['org.cilogon.userinfo']

        return scopes

    idp_whitelist = List(
        help="Deprecated, use `CIlogonOAuthenticator.allowed_idps`",
        config=True,
    )

    allowed_idps = Dict(
        config=True,
        default_value={},
        help="""A dictionary of the only entity IDs that will be allowed to be used as login options.
        See https://cilogon.org/idplist for the list of `EntityIDs` of each IdP.

        It can be used to enable domain stripping, adding prefixes to the usernames and to specify an identity provider specific username claim.

        For example::

            allowed_idps = {
                "https://idpz.utorauth.utoronto.ca/shibboleth": {
                    "username_derivation": {
                        "username_claim": "email",
                        "action": "strip_idp_domain",
                        "domain": "utoronto.ca",
                    }
                },
                "https://github.com/login/oauth/authorize": {
                    "username_derivation": {
                        "username_claim": "username",
                        "action": "prefix",
                        "prefix": "gh"
                    }
                }
                "http://google.com/accounts/o8/id": {
                    "username_derivation": {
                        "username_claim": "username",
                    }
                    "allowed_domains": ["uni.edu", "something.org"]
                }
            }

        Where `username_derivation` defines:
            * :attr:`username_claim`: string
                The claim in the `userinfo` response from which to get the JupyterHub username.
                Examples include: `eppn`, `email`. What keys are available will depend on the scopes requested.
                It will overwrite any value set through CILogonOAuthenticator.username_claim for this identity provider.
            * :attr:`action`: string
                What action to perform on the username. Available options are "strip_idp_domain", which will strip the domain from the username if specified and "prefix", which will prefix the hub username with "prefix:".
            * :attr:`domain:` string
                The domain after "@" which will be stripped from the username if it exists and if the action is "strip_idp_domain".
            * :attr:`prefix`: string
                The prefix which will be added at the beginning of the username followed by a semi-column ":", if the action is "prefix".
            * :attr:`allowed_domains`: string
                It defines which domains will be allowed to login using the specific identity provider.

        Requirements:
            * if `username_derivation.action` is `strip_idp_domain`, then `username_derivation.domain` must also be specified
            * if `username_derivation.action` is `prefix`, then `username_derivation.prefix` must also be specified.
            * `username_claim` must be provided for each idp in `allowed_idps`

        .. versionchanged:: 15.0.0
            `CILogonOAuthenticaor.allowed_idps` changed type from list to dict
        """,
    )

    @validate("allowed_idps")
    def _validate_allowed_idps(self, proposal):
        idps = proposal.value

        for entity_id, username_derivation in idps.items():
            # Validate `username_derivation` config using the schema
            root_dir = os.path.dirname(os.path.abspath(__file__))
            schema_file = os.path.join(root_dir, "schemas", "cilogon-schema.yaml")
            with open(schema_file) as schema_fd:
                schema = yaml.load(schema_fd)
                # Raises useful exception if validation fails
                jsonschema.validate(username_derivation, schema)

            # Make sure allowed_idps containes EntityIDs and not domain names.
            accepted_entity_id_scheme = ["urn", "https", "http"]
            entity_id_scheme = urlparse(entity_id).scheme
            if entity_id_scheme not in accepted_entity_id_scheme:
                # Validate entity ids are the form of: `https://github.com/login/oauth/authorize`
                self.log.error(
                    f"Trying to allow an auth provider: {entity_id}, that doesn't look like a valid CILogon EntityID.",
                )
                raise ValueError(
                    """The keys of `allowed_idps` **must** be CILogon permitted EntityIDs.
                    See https://cilogon.org/idplist for the list of EntityIDs of each IDP.
                    """
                )

        return idps

    strip_idp_domain = Bool(
        False,
        config=True,
        help="""Deprecated, use `CILogonOAuthenticator.allowed_idps["username_derivation"]["action"] = "strip_idp_domain"`
        to enable it and `CIlogonOAuthenticator.allowed_idps["username_derivation"]["domain"]` to list the domain
        which will be stripped
        """,
    )

    idp = Unicode(
        config=True, help="Deprecated, use `CILogonOAuthenticator.shown_idps`."
    )

    shown_idps = List(
        Unicode(),
        config=True,
        help="""A list of identity providers to be shown as login options.
        The `idp` attribute is the SAML Entity ID of the user's selected
        identity provider.

        See https://cilogon.org/include/idplist.xml for the list of identity
        providers supported by CILogon.
        """,
    )

    skin = Unicode(
        config=True,
        help="""The `skin` attribute is the name of the custom CILogon interface skin
        for your application.

        Contact help@cilogon.org to request a custom skin.
        """,
    )

    additional_username_claims = List(
        config=True,
        help="""Additional claims to check if the username_claim fails.

        This is useful for linked identities where not all of them return
        the primary username_claim.
        """,
        default_value=["email"],
    )

    def _get_final_username_claim_list(self, user_info):
        """
        The username claims that will be used to determine the hub username can be set through:
         - `CILogonOAutnenticator.username_claim`, that can be extended through `CILogonOAutnenticator.additional_username_claims`
         or
         - `CILogonOAuthenticator.allowed_idps.<idp>.username_claim`, that
            will overwrite any value set through CILogonOAuthenticator.username_claim
            for this identity provider.

        This function returns the username claim list that will be used for the current user trying to login
        based on the idp that they have selected. If no `CILogonOAutnenticator.allowed_idps` is set, then
        `CILogonOAutnenticator.username_claim` will be used.
        """
        username_claims = [self.username_claim]
        if self.additional_username_claims:
            username_claims.extend(self.additional_username_claims)
        if self.allowed_idps:
            selected_idp = user_info["idp"]
            if selected_idp in self.allowed_idps.keys():
                # The username_claim which should be used for this idp
                return [
                    self.allowed_idps[selected_idp]["username_derivation"][
                        "username_claim"
                    ]
                ]
            else:
                return username_claims
        return username_claims

    def _get_username_from_claim_list(self, user_info, username_claims):
        username = None
        for claim in username_claims:
            username = user_info.get(claim)
            if username:
                break

        return username

    def user_info_to_username(self, user_info):
        username_claims = self._get_final_username_claim_list(user_info)
        username = self._get_username_from_claim_list(user_info, username_claims)

        if not username:
            user_info_keys = sorted(user_info.keys())
            self.log.error(
                f"No username claim in the list at {username_claims} was found in the response {user_info_keys}"
            )
            raise web.HTTPError(500, "Failed to get username from CILogon")

        # Optionally strip idp domain or prefix the username
        if self.allowed_idps:
            selected_idp = user_info["idp"]
            if selected_idp in self.allowed_idps.keys():
                username_derivation = self.allowed_idps[selected_idp][
                    "username_derivation"
                ]
                action = username_derivation.get("action")

                if action == "strip_idp_domain":
                    username = username.split("@", 1)[0]
                elif action == "prefix":
                    prefix = username_derivation["prefix"]
                    username = f"{prefix}:{username}"

        return username

    async def check_allowed(self, username, auth_model):
        """
        Returns True for authorized users, raises errors for users
        denied authorization.

        Overrides the `OAuthenticator.check_allowed` implementation to only allow users
        logging in using a provider that is  part of `allowed_idps`.
        Following this, the user must either be part of `allowed_users` or `allowed_domains`
        to be authorized if either is configured, otherwise all users are
        authorized.
        """
        # Workaround situation when JupyterHub.load_roles or
        # JupyterHub.load_groups is used to create a user, see discussion in
        # https://github.com/jupyterhub/jupyterhub/issues/4461.
        if auth_model is None:
            return True

        # allow admin users recognized via admin_users or update_auth_model
        if auth_model["admin"]:
            return True

        if self.allowed_idps:
            user_info = auth_model["auth_state"][self.user_auth_state_key]
            selected_idp = user_info["idp"]
            if selected_idp not in self.allowed_idps.keys():
                self.log.error(
                    f"Trying to login from an identity provider that was not allowed {selected_idp}",
                )
                raise web.HTTPError(
                    403,
                    "Trying to login using an identity provider that was not allowed",
                )

            allowed_domains = self.allowed_idps[selected_idp].get("allowed_domains")
            if self.allowed_users or allowed_domains:
                if username in self.allowed_users:
                    return True

                if allowed_domains:
                    username_claims = self._get_final_username_claim_list(user_info)
                    username_with_domain = self._get_username_from_claim_list(
                        user_info, username_claims
                    )
                    user_domain = username_with_domain.split("@", 1)[1]
                    if user_domain in allowed_domains:
                        return True
                    else:
                        raise web.HTTPError(
                            403,
                            "Trying to login using a domain that was not allowed",
                        )

                return False
        # Although not recommended, it might be that `allowed_idps` is not specified
        # In this case we need to make sure we still check `allowed_users` and don't assume
        # everyone should be authorized
        elif self.allowed_users:
            if username in self.allowed_users:
                return True
            return False

        # otherwise, authorize all users
        return True


class LocalCILogonOAuthenticator(LocalAuthenticator, CILogonOAuthenticator):

    """A version that mixes in local system user creation"""
