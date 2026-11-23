from . import CheckResult, HealthCheck, Result


class OnlyAdminsCheck(HealthCheck):
    key = "only_admins"

    def perform_check(self, force: bool = False) -> CheckResult:
        from octoprint.access.permissions import Permissions
        from octoprint.server import userManager

        users = userManager.get_all_users()
        non_admins = [
            user
            for user in users
            if not user.has_permission(Permissions.ADMIN) and user.is_active
        ]

        return CheckResult(
            result=Result.INFO if len(non_admins) == 0 else Result.OK,
            context={},
        )
