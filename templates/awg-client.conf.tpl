[Interface]
PrivateKey = ${PEER_PRIV}
Address = ${AWG_NET4}.${PEER_N}/32, ${AWG_NET6}::${PEER_N}/128
DNS = 1.1.1.1, 8.8.8.8
MTU = 1280
Jc = ${AWG_JC}
Jmin = ${AWG_JMIN}
Jmax = ${AWG_JMAX}
S1 = ${AWG_S1}
S2 = ${AWG_S2}
S3 = ${AWG_S3}
S4 = ${AWG_S4}
H1 = ${AWG_H1}
H2 = ${AWG_H2}
H3 = ${AWG_H3}
H4 = ${AWG_H4}
I1 = ${AWG_I1}
I2 = ${AWG_I2}
HeaderProtectionKey = ${AWG_HPK}
ContentPaddingAddition = ${AWG_CPA}
RekeyAfterTime = ${AWG_RAT}
KeepaliveTimeout = ${AWG_KT}
RandomTrailers = on

[Peer]
PublicKey = ${AWG_PUB}
PresharedKey = ${PEER_PSK}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = ${ENDPOINT}
PersistentKeepalive = 25
