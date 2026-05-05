#!/usr/bin/env python3
"""
Palantir Foundry to Veza OAA Integration Script

Collects resource definitions, datasets, workspaces, and access control information
from Palantir Foundry and pushes to Veza via the Open Authorization API (OAA).
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from typing import Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from oaaclient.client import OAAClient, OAAClientError
from oaaclient.templates import CustomApplication, OAAPermission

log = logging.getLogger(__name__)


def _setup_logging(log_level: str = "INFO") -> None:
    """Configure file-only logging with hourly rotation to the logs/ folder."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(script_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%d%m%Y-%H%M")
    script_name = os.path.splitext(os.path.basename(__file__))[0]
    log_file = os.path.join(log_dir, f"{script_name}_{timestamp}.log")

    handler = TimedRotatingFileHandler(
        log_file,
        when="h",
        interval=1,
        backupCount=24,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    root.addHandler(handler)
    root.addHandler(console_handler)


class PalantirFoundryClient:
    """Palantir Foundry API client."""

    def __init__(self, base_url: str, api_token: str):
        """
        Initialize Palantir Foundry client.
        
        Args:
            base_url: Palantir Foundry base URL (e.g., https://westrock.palantirfoundry.com)
            api_token: API token for authentication
        """
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._filesystem_cache: Optional[Tuple[List, List, List]] = None
        self._test_connection()

    def _test_connection(self) -> None:
        """Verify the API token is accepted by Palantir Foundry."""
        try:
            response = requests.get(
                f"{self.base_url}/api/v2/admin/users",
                headers=self.headers,
                params={"pageSize": 1},
                timeout=10,
            )
            response.raise_for_status()
            log.info("Successfully authenticated with Palantir Foundry at %s", self.base_url)
        except requests.RequestException as e:
            log.error("Failed to connect to Palantir Foundry: %s", e)
            sys.exit(1)

    def _make_request(
        self, method: str, endpoint: str, params: Optional[Dict] = None
    ) -> Dict:
        """Make an API request to Palantir Foundry and return the JSON body."""
        # urljoin requires the base to end with '/' and the path to have no leading '/'
        url = self.base_url.rstrip("/") + "/" + endpoint.lstrip("/")
        try:
            response = requests.request(
                method,
                url,
                headers=self.headers,
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            log.error("API request failed (%s %s): %s", method, endpoint, e)
            raise

    def get_paginated_results(
        self,
        endpoint: str,
        items_key: Optional[str] = None,
        params: Optional[Dict] = None,
    ) -> List[Dict]:
        """Fetch all pages from a paginated Foundry endpoint."""
        results = []
        page_token = None
        params = dict(params or {})

        while True:
            if page_token:
                params["pageToken"] = page_token

            try:
                data = self._make_request("GET", endpoint, params=params)

                # Resolve items key: use caller hint, then auto-detect from response keys
                key = items_key
                if key is None:
                    for candidate in ("datasets", "projects", "workspaces", "resources", "items", "data"):
                        if candidate in data:
                            key = candidate
                            break

                items = data.get(key, []) if key else []
                results.extend(items)

                page_token = data.get("nextPageToken")
                if not page_token:
                    break

            except requests.RequestException:
                log.warning(
                    "Pagination stopped at %d items due to request error", len(results)
                )
                break

        return results

    def _list_spaces(self) -> List[Dict]:
        """Fetch all Spaces from the Palantir Foundry v2 Filesystem API."""
        try:
            return self.get_paginated_results(
                "/api/v2/filesystem/spaces", items_key="data", params={"preview": "true"}
            )
        except requests.RequestException:
            log.error("Failed to fetch spaces")
            return []

    def get_folder_children(self, folder_rid: str) -> List[Dict]:
        """List all direct children of a folder or space (v2 Filesystem API)."""
        try:
            return self.get_paginated_results(
                f"/api/v2/filesystem/folders/{folder_rid}/children",
                items_key="data",
                params={"preview": "true"},
            )
        except requests.RequestException as e:
            log.debug("Could not list children of %s: %s", folder_rid, e)
            return []

    def _traverse_folder(
        self,
        folder_rid: str,
        projects: List[Dict],
        datasets: List[Dict],
        resources: List[Dict],
        depth: int = 0,
        max_depth: int = 5,
    ) -> None:
        """Recursively traverse a folder collecting projects, datasets, and other resources."""
        if depth > max_depth:
            log.debug("Max traversal depth reached at %s", folder_rid)
            return
        for child in self.get_folder_children(folder_rid):
            rid = child.get("rid", "")
            rtype = child.get("type", "")
            if child.get("trashStatus", "NOT_TRASHED") != "NOT_TRASHED":
                continue
            if rtype == "FOUNDRY_DATASET":
                datasets.append(child)
            elif rtype == "COMPASS_FOLDER":
                # A resource is a Project if its rid equals its projectRid field
                if rid and rid == child.get("projectRid"):
                    projects.append(child)
                # Recurse into both regular folders and projects
                self._traverse_folder(
                    rid, projects, datasets, resources, depth + 1, max_depth
                )
            elif rtype:
                resources.append(child)

    def _discover_filesystem(self) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        """Traverse all spaces once and return (projects, datasets, resources). Results are cached."""
        if self._filesystem_cache is not None:
            return self._filesystem_cache
        spaces = self._list_spaces()
        projects: List[Dict] = []
        datasets: List[Dict] = []
        resources: List[Dict] = []
        for space in spaces:
            space_rid = space.get("rid", "")
            if space_rid:
                self._traverse_folder(space_rid, projects, datasets, resources)
        log.info(
            "Filesystem traversal complete: %d projects, %d datasets, %d other resources",
            len(projects), len(datasets), len(resources),
        )
        self._filesystem_cache = (projects, datasets, resources)
        return self._filesystem_cache

    def get_workspaces(self) -> List[Dict]:
        """Fetch all Spaces (top-level containers) from Palantir Foundry."""
        log.info("Fetching spaces...")
        results = self._list_spaces()
        log.info("Retrieved %d spaces", len(results))
        return results

    def get_projects(self) -> List[Dict]:
        """Fetch all Projects from the Palantir Foundry filesystem."""
        log.info("Fetching projects...")
        projects, _, _ = self._discover_filesystem()
        log.info("Retrieved %d projects", len(projects))
        return projects

    def get_datasets(self) -> List[Dict]:
        """Fetch all Datasets (type=FOUNDRY_DATASET) from the Palantir Foundry filesystem."""
        log.info("Fetching datasets...")
        _, datasets, _ = self._discover_filesystem()
        log.info("Retrieved %d datasets", len(datasets))
        return datasets

    def get_resources(self) -> List[Dict]:
        """Fetch all non-dataset, non-folder resources from the Palantir Foundry filesystem."""
        log.info("Fetching resources...")
        _, _, resources = self._discover_filesystem()
        log.info("Retrieved %d resources", len(resources))
        return resources

    def get_access_policies(self, resource_id: str) -> List[Dict]:
        """Fetch role grants for a resource via the v2 Filesystem API."""
        try:
            return self.get_paginated_results(
                f"/api/v2/filesystem/resources/{resource_id}/roles",
                items_key="data",
            )
        except requests.RequestException as e:
            log.debug("No roles for resource %s: %s", resource_id, e)
            return []

    def get_users(self) -> List[Dict]:
        """Fetch all users from Palantir Foundry."""
        log.info("Fetching users...")
        try:
            results = self.get_paginated_results(
                "/api/v2/admin/users", items_key="data"
            )
            log.info("Retrieved %d users", len(results))
            return results
        except requests.RequestException:
            log.error("Failed to fetch users")
            return []

    def get_groups(self) -> List[Dict]:
        """Fetch all groups from Palantir Foundry."""
        log.info("Fetching groups...")
        try:
            results = self.get_paginated_results(
                "/api/v2/admin/groups", items_key="data"
            )
            log.info("Retrieved %d groups", len(results))
            return results
        except requests.RequestException:
            log.error("Failed to fetch groups")
            return []

    def get_group_members(self, group_id: str) -> List[Dict]:
        """Fetch all members (users and nested groups) of a specific group."""
        try:
            results = self.get_paginated_results(
                f"/api/v2/admin/groups/{group_id}/groupMembers", items_key="data"
            )
            return results
        except requests.RequestException as e:
            log.debug("No members found for group %s: %s", group_id, e)
            return []

    def get_admin_roles(self) -> Dict[str, str]:
        """Fetch all Foundry roles and return a {roleId: displayName} mapping."""
        try:
            roles = self.get_paginated_results("/api/v2/admin/roles", items_key="data")
            return {r["id"]: r.get("displayName", r["id"]) for r in roles if "id" in r}
        except requests.RequestException as e:
            log.warning("Could not fetch admin roles (role names will be inferred from ID): %s", e)
            return {}

    def get_ontology_programs(self) -> List[Dict]:
        """Fetch AIP programs and action types from the Palantir Foundry Ontology API."""
        programs: List[Dict] = []
        try:
            ontologies = self.get_paginated_results("/api/v2/ontologies", items_key="data")
            for ontology in ontologies:
                ont_rid = ontology.get("rid") or ontology.get("ontologyRid")
                if not ont_rid:
                    continue
                try:
                    actions = self.get_paginated_results(
                        f"/api/v2/ontologies/{ont_rid}/actionTypes",
                        items_key="data",
                    )
                    for action in actions:
                        action.setdefault("type", "AIP_PROGRAM")
                        action["_ontologyRid"] = ont_rid
                    programs.extend(actions)
                except requests.RequestException as e:
                    log.debug("Could not fetch action types for ontology %s: %s", ont_rid, e)
        except requests.RequestException as e:
            log.warning("Could not fetch ontologies (AIP programs skipped): %s", e)
        log.info("Retrieved %d ontology programs", len(programs))
        return programs


VEZA_MAX_FIELD_LEN = 512


def _apply_resource_properties(resource, data: Dict) -> None:
    """Set description on an OAA resource object if present in the source data."""
    if description := data.get("description"):
        resource.description = description[:VEZA_MAX_FIELD_LEN]


def _map_role_to_oaa_permissions(role_id: str, role_display_name: str = "") -> List[str]:
    """Map a Foundry role ID (e.g. compass:edit, marketplace-installation:manage) to OAA permission names."""
    name = role_display_name.lower() if role_display_name else role_id.lower()
    if any(kw in name for kw in ("manage", "owner", "administer", "admin")):
        return ["Manage Access"]
    if any(kw in name for kw in ("edit", "contributor", "write", "build")):
        return ["Edit Logic", "Run Model", "View Data"]
    if any(kw in name for kw in ("view", "viewer", "read", "discover")):
        return ["View Data"]
    return ["View Data"]


def _role_to_action_permissions(role_display_name: str) -> List[str]:
    """Return action-item permission names granted by a Foundry role."""
    name = role_display_name.lower()
    if any(kw in name for kw in ("owner", "administer", "admin")):
        return ["Edit Logic", "Run Model", "View Data", "Manage Access"]
    if any(kw in name for kw in ("editor", "write", "edit")):
        return ["Edit Logic", "Run Model", "View Data"]
    if any(kw in name for kw in ("viewer", "view", "read", "discover")):
        return ["View Data"]
    return ["View Data"]


def build_oaa_payload(
    foundry_data: Dict,
    provider_name: str = "Palantir Foundry",
    datasource_name: str = "palantir-foundry",
) -> CustomApplication:
    """Build OAA CustomApplication from Palantir Foundry data.

    Permission model: User > Group > Role > Action Permissions > Resource
      - Users are members of Groups
      - Groups are assigned Roles on Project resources
      - Roles define fine-grained Action Permissions (Edit Logic, Run Model, View Data)
      - Projects contain Programs and Datasets as sub-resources
      - Direct user-to-resource grants are intentionally skipped
    """
    app = CustomApplication(
        name=datasource_name,
        application_type=provider_name,
    )

    # ── Action-level custom permissions ───────────────────────────────────────
    app.add_custom_permission("View Data",     [OAAPermission.DataRead, OAAPermission.MetadataRead])
    app.add_custom_permission("Edit Logic",    [OAAPermission.DataWrite, OAAPermission.MetadataWrite])
    app.add_custom_permission("Run Model",     [OAAPermission.NonData])
    app.add_custom_permission("Manage Access", [
        OAAPermission.DataRead, OAAPermission.DataWrite,
        OAAPermission.MetadataRead, OAAPermission.MetadataWrite, OAAPermission.NonData,
    ])

    resource_lookup: Dict[str, object] = {}
    user_lookup: Dict[str, object] = {}
    group_lookup: Dict[str, object] = {}
    project_resource_lookup: Dict[str, object] = {}

    # ── Local roles ───────────────────────────────────────────────────────────
    # Each Foundry role becomes an OAA local role that carries action permissions.
    # role_name_lookup: Foundry roleId → OAA role display name
    log.info("Creating local roles from Foundry role definitions...")
    role_name_lookup: Dict[str, str] = {}
    seen_role_names: set = set()
    for role_id, role_display in foundry_data.get("role_lookup", {}).items():
        role_name = role_display or role_id
        if role_name not in seen_role_names:
            action_perms = _role_to_action_permissions(role_name)
            app.add_local_role(role_name, permissions=action_perms)
            seen_role_names.add(role_name)
            log.debug("Added role: %s → %s", role_name, action_perms)
        role_name_lookup[role_id] = role_name
    log.info("Created %d local roles", len(seen_role_names))

    # ── Users ─────────────────────────────────────────────────────────────────
    log.info("Processing users...")
    for user in foundry_data.get("users", []):
        uid = user.get("id") or user.get("userId")
        if not uid:
            log.warning("User missing id — skipping")
            continue
        username = user.get("username") or user.get("login") or uid
        oaa_user = app.add_local_user(username, unique_id=uid)
        email = user.get("email")
        if email:
            oaa_user.email = email
            oaa_user.add_identity(email)
        elif "@" in username:
            oaa_user.email = username
            oaa_user.add_identity(username)
        user_lookup[uid] = oaa_user
        log.debug("Added user: %s (%s)", username, uid)

    # ── Groups ────────────────────────────────────────────────────────────────
    log.info("Processing groups...")
    for group in foundry_data.get("groups", []):
        gid = group.get("id") or group.get("groupId")
        if not gid:
            log.warning("Group missing id — skipping")
            continue
        name = group.get("name") or group.get("displayName") or gid
        oaa_group = app.add_local_group(name, unique_id=gid)
        group_lookup[gid] = oaa_group
        log.debug("Added group: %s (%s)", name, gid)

    # ── Group memberships ─────────────────────────────────────────────────────
    log.info("Processing group memberships...")
    membership_count = 0
    for membership in foundry_data.get("group_memberships", []):
        group_id = membership.get("group_id")
        # v2 admin/groups/{id}/groupMembers returns {principalType, principalId}
        m_type = membership.get("principalType", "").upper()
        m_id = membership.get("principalId")
        if not group_id or not m_id:
            continue
        oaa_group = group_lookup.get(group_id)
        if oaa_group is None:
            continue
        if m_type == "USER":
            oaa_user = user_lookup.get(m_id)
            if oaa_user:
                oaa_user.add_group(group_id)
                membership_count += 1
        elif m_type in ("GROUP", "TEAM"):
            child_group = group_lookup.get(m_id)
            if child_group:
                child_group.add_group(group_id)
                membership_count += 1
    log.info("Linked %d group memberships", membership_count)

    # ── Workspaces (top-level resources) ──────────────────────────────────────
    log.info("Processing workspaces...")
    for workspace in foundry_data.get("workspaces", []):
        rid = workspace.get("rid") or workspace.get("id")
        if not rid:
            log.warning("Workspace missing rid/id — skipping")
            continue
        name = (workspace.get("displayName") or workspace.get("name") or rid)[:VEZA_MAX_FIELD_LEN]
        resource = app.add_resource(name=name, resource_type="Workspace", unique_id=rid)
        resource_lookup[rid] = resource
        _apply_resource_properties(resource, workspace)
        log.debug("Added workspace: %s (%s)", name, rid)

    # ── Projects (top-level resources, parent of Programs and Datasets) ────────
    log.info("Processing projects...")
    for project in foundry_data.get("projects", []):
        rid = project.get("rid") or project.get("id")
        if not rid:
            log.warning("Project missing rid/id — skipping")
            continue
        name = (project.get("displayName") or project.get("name") or rid)[:VEZA_MAX_FIELD_LEN]
        resource = app.add_resource(name=name, resource_type="Project", unique_id=rid)
        project_resource_lookup[rid] = resource
        resource_lookup[rid] = resource
        _apply_resource_properties(resource, project)
        log.debug("Added project: %s (%s)", name, rid)

    # ── Datasets (sub-resources under their parent project) ───────────────────
    log.info("Processing datasets...")
    for dataset in foundry_data.get("datasets", []):
        rid = dataset.get("rid") or dataset.get("id")
        if not rid:
            log.warning("Dataset missing rid/id — skipping")
            continue
        name = (dataset.get("displayName") or dataset.get("name") or rid)[:VEZA_MAX_FIELD_LEN]
        parent_rid = dataset.get("projectRid")
        parent_project = project_resource_lookup.get(parent_rid) if parent_rid else None
        if parent_project:
            sub = parent_project.add_sub_resource(name=name, resource_type="Dataset", unique_id=rid)
            resource_lookup[rid] = sub
            if row_count := dataset.get("rowCount"):
                sub.add_property("row_count", str(row_count), "str")
        else:
            resource = app.add_resource(name=name, resource_type="Dataset", unique_id=rid)
            resource_lookup[rid] = resource
            if row_count := dataset.get("rowCount"):
                resource.add_property("row_count", str(row_count), "str")
        log.debug("Added dataset: %s (%s)", name, rid)

    # ── Programs / AIP models (sub-resources under their parent project) ───────
    log.info("Processing ontology programs...")
    for program in foundry_data.get("programs", []):
        rid = program.get("rid") or program.get("id") or program.get("apiName")
        if not rid:
            log.warning("Program missing id — skipping")
            continue
        name = (program.get("displayName") or program.get("name") or rid)[:VEZA_MAX_FIELD_LEN]
        parent_rid = program.get("projectRid") or program.get("_ontologyRid")
        parent_project = project_resource_lookup.get(parent_rid) if parent_rid else None
        if parent_project:
            sub = parent_project.add_sub_resource(name=name, resource_type="Program", unique_id=rid)
            resource_lookup[rid] = sub
        else:
            resource = app.add_resource(name=name, resource_type="Program", unique_id=rid)
            resource_lookup[rid] = resource
        log.debug("Added program: %s (%s)", name, rid)

    # ── Generic resources (sub-resource if they have projectRid, else top-level)
    log.info("Processing resources...")
    for res in foundry_data.get("resources", []):
        rid = res.get("rid") or res.get("id")
        if not rid:
            log.warning("Resource missing rid/id — skipping")
            continue
        # Skip action types already handled by the programs loop
        if rid in resource_lookup:
            log.debug("Skipping resource %s — already added as a program", rid)
            continue
        name = (res.get("displayName") or res.get("name") or rid)[:VEZA_MAX_FIELD_LEN]
        rtype = res.get("type", "Resource")
        rtype_label = "Program" if any(
            kw in rtype.upper() for kw in ("FUNCTION", "MODEL", "PIPELINE", "PROGRAM", "AIP")
        ) else rtype
        parent_rid = res.get("projectRid")
        parent_project = project_resource_lookup.get(parent_rid) if parent_rid else None
        if parent_project:
            sub = parent_project.add_sub_resource(name=name, resource_type=rtype_label, unique_id=rid)
            resource_lookup[rid] = sub
            _apply_resource_properties(sub, res)
        else:
            resource = app.add_resource(name=name, resource_type=rtype_label, unique_id=rid)
            resource_lookup[rid] = resource
            _apply_resource_properties(resource, res)
        log.debug("Added resource: %s (%s)", name, rid)

    # ── Access policies ───────────────────────────────────────────────────────
    # v2 filesystem/resources/{rid}/roles returns ResourceRole objects:
    #   {resourceRolePrincipal: {type, principalId, principalType}, roleId}
    #
    # Permission model: User > Group > Role > Action Permissions > Project
    # Groups are assigned named Roles on Project resources; Roles carry the
    # action permissions ("Edit Logic", "Run Model", "View Data").
    # Direct USER grants are intentionally skipped — access flows via group membership.
    log.info("Processing access policies (group → role → resource)...")
    total_permissions = 0
    skipped_direct_user_grants = 0
    for resource_id, policies in foundry_data.get("access_policies", {}).items():
        oaa_resource = resource_lookup.get(resource_id)
        if oaa_resource is None:
            continue
        for policy in policies:
            rr_principal = policy.get("resourceRolePrincipal", {})
            principal_type_discriminator = rr_principal.get("type", "")
            role_id = policy.get("roleId", "")

            if principal_type_discriminator == "everyone":
                continue

            p_id = rr_principal.get("principalId")
            p_type = rr_principal.get("principalType", "").upper()
            if not p_id:
                continue

            if p_type == "USER":
                oaa_user = user_lookup.get(p_id)
                if oaa_user:
                    role_display = foundry_data.get("role_lookup", {}).get(role_id, "")
                    for perm in _map_role_to_oaa_permissions(role_id, role_display):
                        oaa_user.add_permission(perm, resources=[oaa_resource])
                    total_permissions += 1
                else:
                    log.debug(
                        "Skipping direct user grant for unknown user %s, role %s, resource %s",
                        p_id, role_id, resource_id,
                    )
                    skipped_direct_user_grants += 1
            elif p_type == "GROUP":
                if p_id not in group_lookup:
                    group_lookup[p_id] = app.add_local_group(p_id, unique_id=p_id)
                oaa_group = group_lookup[p_id]
                role_name = role_name_lookup.get(role_id)
                if role_name:
                    oaa_group.add_role(role_name, resources=[oaa_resource])
                else:
                    role_display = foundry_data.get("role_lookup", {}).get(role_id, "")
                    for perm in _map_role_to_oaa_permissions(role_id, role_display):
                        oaa_group.add_permission(perm, resources=[oaa_resource])
                total_permissions += 1

    if skipped_direct_user_grants:
        log.info(
            "Skipped %d direct user permission grant(s) — access flows via group membership",
            skipped_direct_user_grants,
        )

    log.info(
        "Payload: %d workspaces, %d projects, %d datasets, %d programs, %d resources, "
        "%d users, %d groups, %d roles, %d memberships, %d permissions",
        len(foundry_data.get("workspaces", [])),
        len(foundry_data.get("projects", [])),
        len(foundry_data.get("datasets", [])),
        len(foundry_data.get("programs", [])),
        len(foundry_data.get("resources", [])),
        len(user_lookup),
        len(group_lookup),
        len(seen_role_names),
        membership_count,
        total_permissions,
    )
    return app


def push_to_veza(
    veza_url: str,
    veza_api_key: str,
    provider_name: str,
    datasource_name: str,
    app: CustomApplication,
    save_json: bool = False,
) -> None:
    """Push the OAA payload to Veza. Exits with code 1 on failure."""
    if save_json:
        out_path = f"{datasource_name.lower().replace(' ', '_')}_oaa_payload.json"
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(app.get_payload(), fh, indent=2)
        log.info("OAA payload saved to %s", out_path)

    veza_con = OAAClient(url=veza_url, token=veza_api_key)
    try:
        response = veza_con.push_application(
            provider_name=provider_name,
            data_source_name=datasource_name,
            application_object=app,
            create_provider=True,
        )
        if response and response.get("warnings"):
            for w in response["warnings"]:
                log.warning("Veza warning: %s", w)
        log.info("Successfully pushed to Veza")
    except OAAClientError as e:
        log.error("Veza push failed: %s — %s (HTTP %s)", e.error, e.message, e.status_code)
        if hasattr(e, "details"):
            for detail in e.details:
                log.error("  Detail: %s", detail)
        sys.exit(1)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Palantir Foundry to Veza OAA Integration",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # Source
    parser.add_argument("--foundry-url", help="Palantir Foundry base URL (overrides FOUNDRY_BASE_URL)")
    parser.add_argument("--foundry-token", help="Palantir Foundry API token (overrides FOUNDRY_API_TOKEN)")
    # Veza
    parser.add_argument("--veza-url", help="Veza tenant URL (overrides VEZA_URL)")
    parser.add_argument("--veza-api-key", help="Veza API key (overrides VEZA_API_KEY)")
    parser.add_argument("--provider-name", default="Palantir Foundry", help="Provider name in Veza")
    parser.add_argument("--datasource-name", default="palantir-foundry", help="Datasource name in Veza")
    # Runtime
    parser.add_argument("--env-file", default=".env", help="Path to .env configuration file")
    parser.add_argument("--dry-run", action="store_true", help="Build payload without pushing to Veza")
    parser.add_argument("--save-json", action="store_true", help="Save OAA payload as JSON for inspection")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    return parser.parse_args()


def _load_config(args: argparse.Namespace) -> Dict[str, Optional[str]]:
    """Load configuration with precedence: CLI arg → env var → .env file."""
    if os.path.exists(args.env_file):
        load_dotenv(args.env_file)
        log.info("Loaded .env from %s", args.env_file)
    else:
        log.info(".env not found at %s — using environment variables only", args.env_file)
    return {
        "foundry_base_url": args.foundry_url or os.getenv("FOUNDRY_BASE_URL"),
        "foundry_api_token": args.foundry_token or os.getenv("FOUNDRY_API_TOKEN"),
        "veza_url": args.veza_url or os.getenv("VEZA_URL"),
        "veza_api_key": args.veza_api_key or os.getenv("VEZA_API_KEY"),
    }


def main() -> None:
    """Main entry point."""
    args = _parse_args()
    _setup_logging(args.log_level)

    print(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  Palantir Foundry → Veza OAA Integration\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    config = _load_config(args)

    if not config["foundry_base_url"] or not config["foundry_api_token"]:
        log.error("Missing required config: FOUNDRY_BASE_URL and FOUNDRY_API_TOKEN")
        sys.exit(1)

    if not args.dry_run and (not config["veza_url"] or not config["veza_api_key"]):
        log.error("Missing required config: VEZA_URL and VEZA_API_KEY")
        sys.exit(1)

    # Connect and collect data
    print("[1/5] Connecting to Palantir Foundry...")
    log.info("Connecting to Palantir Foundry at %s", config["foundry_base_url"])
    foundry = PalantirFoundryClient(
        base_url=config["foundry_base_url"],
        api_token=config["foundry_api_token"],
    )

    print("[2/5] Collecting data...")
    log.info("Collecting data from Palantir Foundry...")
    workspaces = foundry.get_workspaces()
    projects = foundry.get_projects()
    datasets = foundry.get_datasets()
    resources = foundry.get_resources()
    programs = foundry.get_ontology_programs()
    users = foundry.get_users()
    groups = foundry.get_groups()
    role_lookup = foundry.get_admin_roles()
    log.info("Fetched %d role definitions", len(role_lookup))

    # Link each ontology action type back to its parent project.
    # The filesystem traversal captures ACTIONS_ACTIONTYPE resources with a
    # projectRid; we use that to set program["projectRid"] so build_oaa_payload
    # can nest programs under the correct project resource.
    action_type_project: Dict[str, str] = {
        res["rid"]: res["projectRid"]
        for res in resources
        if res.get("rid", "").startswith("ri.actions.") and res.get("projectRid")
    }
    for program in programs:
        prog_rid = program.get("rid") or program.get("id") or program.get("apiName")
        if prog_rid and prog_rid in action_type_project and "projectRid" not in program:
            program["projectRid"] = action_type_project[prog_rid]
            log.debug("Linked program %s to project %s", prog_rid, action_type_project[prog_rid])
    log.info(
        "Linked %d of %d programs to parent projects via filesystem traversal",
        sum(1 for p in programs if p.get("projectRid")),
        len(programs),
    )

    # Fetch group memberships
    log.info("Fetching group memberships...")
    group_memberships: List[Dict] = []
    for group in groups:
        gid = group.get("id") or group.get("groupId")
        if gid:
            for member in foundry.get_group_members(gid):
                group_memberships.append({"group_id": gid, **member})
    log.info("Retrieved %d group membership entries", len(group_memberships))

    # Fetch access policies for every discovered entity
    print("[3/5] Fetching access policies...")
    access_policies: Dict[str, List] = {}
    for entity in workspaces + projects + datasets + resources:
        rid = entity.get("rid") or entity.get("id")
        if rid:
            policies = foundry.get_access_policies(rid)
            if policies:
                access_policies[rid] = policies

    # Programs (ontology action types) need their own ACL fetch.
    # Palantir may grant access at the action-type level directly, or inherit
    # from the parent ontology. We try direct first; if empty we fall back to
    # the ontology-level grant so group→role→program links are populated.
    _ontology_policy_cache: Dict[str, List] = {}
    for program in programs:
        prog_rid = program.get("rid") or program.get("id") or program.get("apiName")
        ont_rid = program.get("_ontologyRid")
        if not prog_rid:
            continue
        policies = foundry.get_access_policies(prog_rid)
        if policies:
            access_policies[prog_rid] = policies
        elif ont_rid:
            if ont_rid not in _ontology_policy_cache:
                _ontology_policy_cache[ont_rid] = foundry.get_access_policies(ont_rid)
            if _ontology_policy_cache[ont_rid]:
                access_policies[prog_rid] = _ontology_policy_cache[ont_rid]

    foundry_data = {
        "workspaces": workspaces,
        "projects": projects,
        "datasets": datasets,
        "resources": resources,
        "programs": programs,
        "users": users,
        "groups": groups,
        "group_memberships": group_memberships,
        "access_policies": access_policies,
        "role_lookup": role_lookup,
    }

    print("[4/5] Building OAA payload...")
    log.info("Building OAA payload...")
    app = build_oaa_payload(foundry_data, args.provider_name, args.datasource_name)

    if args.dry_run:
        print("[5/5] Dry run complete.")
        log.info("[DRY RUN] Payload built — skipping Veza push")
        if args.save_json:
            out = f"{args.datasource_name.lower().replace(' ', '_')}_oaa_payload.json"
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(app.get_payload(), fh, indent=2)
            log.info("Payload saved to %s", out)
        sys.exit(0)

    print("[5/5] Pushing to Veza...")
    push_to_veza(
        veza_url=config["veza_url"],
        veza_api_key=config["veza_api_key"],
        provider_name=args.provider_name,
        datasource_name=args.datasource_name,
        app=app,
        save_json=args.save_json,
    )
    print("Done.")
    log.info("Integration completed successfully")


if __name__ == "__main__":
    main()
