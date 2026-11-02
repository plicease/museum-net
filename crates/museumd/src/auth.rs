//! Who is on the other end of the socket. The kernel reports the peer's user,
//! so a client can't claim to be anyone else.

use std::ffi::CString;

use nix::unistd::{Gid, Uid, User, getgrouplist};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Peer {
    pub name: String,
    pub may_mutate: bool,
}

/// Socket permissions decide who can read. Changes also need root or the admin
/// group, looked up per connection so a removed member loses access at once.
pub fn identify(uid: u32, admin_group: Gid) -> Peer {
    let uid = Uid::from_raw(uid);
    let user = User::from_uid(uid).ok().flatten();
    Peer {
        name: user
            .as_ref()
            .map_or_else(|| format!("uid {uid}"), |user| user.name.clone()),
        may_mutate: uid.is_root() || user.is_some_and(|user| belongs_to(&user, admin_group)),
    }
}

fn belongs_to(user: &User, group: Gid) -> bool {
    CString::new(user.name.as_str())
        .ok()
        .and_then(|name| getgrouplist(&name, user.gid).ok())
        .is_some_and(|groups| groups.contains(&group))
}

#[cfg(test)]
mod tests {
    use super::*;

    const NO_SUCH_GROUP: Gid = Gid::from_raw(0xfffe);

    #[test]
    fn root_may_always_mutate() {
        assert!(identify(0, NO_SUCH_GROUP).may_mutate);
    }

    #[test]
    fn an_unknown_user_is_named_by_number_and_may_not_mutate() {
        let peer = identify(0xfffd, NO_SUCH_GROUP);
        assert_eq!(peer.name, "uid 65533");
        assert!(!peer.may_mutate);
    }
}
