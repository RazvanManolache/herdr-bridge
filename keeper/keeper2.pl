use IO::Socket::UNIX;
use Fcntl qw(:flock);
my $profile_dir = $ENV{USERPROFILE} or die "USERPROFILE required\n";
$profile_dir =~ tr{\\}{/};
$profile_dir =~ s{^([A-Za-z]):}{'/' . lc($1)}e;
open my $lock, ">>", "$profile_dir/.herdr-bridge-emu/keeper.lock" or die "lock unavailable\n";
flock($lock, LOCK_EX | LOCK_NB) or exit 0;
my $sock = "$profile_dir/.config/herdr/herdr.sock";
# This endpoint is only a filesystem probe, never the Herdr control socket.
# Do not record bytes from accidental connections.
unlink $sock;
my $server = IO::Socket::UNIX->new(Type => SOCK_STREAM, Local => $sock, Listen => 4)
    or die "placeholder socket unavailable\n";
while (my $client = $server->accept()) { close $client; }
